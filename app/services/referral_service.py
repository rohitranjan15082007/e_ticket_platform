"""Referral program configuration, safe attribution, and pending rewards."""

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_paise
from app.core.permissions import RoleName
from app.core.transaction_locks import lock_key_for_transaction
from app.exceptions import AuthorizationError, ConflictError, InvariantViolationError, ValidationError
from app.models.order import Order, OrderStatus
from app.models.referral import (
    Referral,
    ReferralProfile,
    ReferralProgram,
    ReferralProgramStatus,
    ReferralReward,
    ReferralRewardRecipient,
    ReferralRewardStatus,
    ReferralStatus,
)
from app.models.user import IdempotencyRecord, User


_CODE = re.compile(r"[A-Z0-9][A-Z0-9_-]{2,63}")


@dataclass(slots=True)
class ReferralMutationResult:
    resource: Referral | ReferralProfile | ReferralProgram
    response_payload: dict[str, object]
    replayed: bool


class ReferralService:
    """Keeps referral attribution separate from wallet-credit authorization."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)

    async def create_program(
        self,
        *,
        actor_user_id: UUID,
        name: str,
        description: str | None,
        referrer_reward_paise: int,
        referred_reward_paise: int,
        minimum_order_paise: int,
        starts_at: datetime | None,
        ends_at: datetime | None,
        idempotency_key: str,
        commit: bool,
    ) -> ReferralMutationResult:
        """Create a DRAFT fixed-reward program; activation is separate."""

        await self._require_admin(actor_user_id)
        values = self._program_values(
            name=name,
            description=description,
            referrer_reward_paise=referrer_reward_paise,
            referred_reward_paise=referred_reward_paise,
            minimum_order_paise=minimum_order_paise,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        scope = f"admin:{actor_user_id}:referral-program.create"
        request_fingerprint = fingerprint({"action": "create", **values})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_program(replay)
        program = ReferralProgram(
            id=uuid4(),
            name=values["name"],
            description=values["description"],
            referrer_reward_paise=values["referrer_reward_paise"],
            referred_reward_paise=values["referred_reward_paise"],
            minimum_order_paise=values["minimum_order_paise"],
            starts_at=values["starts_at"],
            ends_at=values["ends_at"],
            status=ReferralProgramStatus.DRAFT,
            created_by_user_id=actor_user_id,
        )
        self.session.add(program)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="referral_program",
            resource_id=program.id,
            status_code=201,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="referral_program",
            entity_id=program.id,
            action="REFERRAL_PROGRAM_CREATED",
            before_state=None,
            after_state=self.program_state(program),
        )
        await self.session.flush()
        response = self.program_snapshot(program)
        record.response_payload = response
        await self._finish(commit=commit)
        return ReferralMutationResult(program, response, False)

    async def activate_program(
        self,
        *,
        program_id: UUID,
        actor_user_id: UUID,
        idempotency_key: str,
        commit: bool,
    ) -> ReferralMutationResult:
        """Activate only one current program and preserve prior program snapshots."""

        await self._require_admin(actor_user_id)
        program = await self._locked_program(program_id)
        scope = f"admin:{actor_user_id}:referral-program.activate"
        request_fingerprint = fingerprint({"action": "activate", "program_id": program_id})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_program(replay)
        await lock_key_for_transaction(self.session, "referral-program-active")
        if program.status == ReferralProgramStatus.DISABLED:
            raise ConflictError("REFERRAL_PROGRAM_DISABLED", "Disabled program cannot be activated")
        now = datetime.now(timezone.utc)
        self._assert_program_window(program, now=now)
        active = await self.session.scalar(
            select(ReferralProgram)
            .where(ReferralProgram.status == ReferralProgramStatus.ACTIVE)
            .with_for_update()
        )
        if active is not None and active.id != program.id:
            raise ConflictError("REFERRAL_PROGRAM_ALREADY_ACTIVE", "Disable the active referral program first")
        before = self.program_state(program)
        program.status = ReferralProgramStatus.ACTIVE
        program.activated_by_user_id = actor_user_id
        program.activated_at = now
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="referral_program",
            resource_id=program.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="referral_program",
            entity_id=program.id,
            action="REFERRAL_PROGRAM_ACTIVATED",
            before_state=before,
            after_state=self.program_state(program),
        )
        await self.session.flush()
        response = self.program_snapshot(program)
        record.response_payload = response
        await self._finish(commit=commit)
        return ReferralMutationResult(program, response, False)

    async def create_profile(
        self, *, user_id: UUID, idempotency_key: str, commit: bool
    ) -> ReferralMutationResult:
        """Create one generated, non-secret referral code for an active user."""

        await self._require_active_user(user_id, lock=True)
        scope = f"user:{user_id}:referral-profile.create"
        request_fingerprint = fingerprint({"action": "create_profile", "user_id": user_id})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_profile(replay)
        existing = await self.session.scalar(
            select(ReferralProfile).where(ReferralProfile.user_id == user_id).with_for_update()
        )
        if existing is not None:
            response = self.profile_snapshot(existing)
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="referral_profile",
                resource_id=existing.id,
            )
            record.response_payload = response
            await self._finish(commit=commit)
            return ReferralMutationResult(existing, response, True)
        profile = await self._new_profile(user_id)
        self.session.add(profile)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="referral_profile",
            resource_id=profile.id,
            status_code=201,
        )
        self.audit.record(
            actor_user_id=user_id,
            entity_type="referral_profile",
            entity_id=profile.id,
            action="REFERRAL_PROFILE_CREATED",
            before_state=None,
            after_state=self.profile_state(profile),
        )
        await self.session.flush()
        response = self.profile_snapshot(profile)
        record.response_payload = response
        await self._finish(commit=commit)
        return ReferralMutationResult(profile, response, False)

    async def disable_program(
        self, *, program_id: UUID, actor_user_id: UUID, reason: str,
        idempotency_key: str, commit: bool,
    ) -> ReferralMutationResult:
        await self._require_admin(actor_user_id)
        reason = self._text(reason, field="reason", maximum=500)
        scope = f"admin:{actor_user_id}:referral-program.disable"
        digest = fingerprint({"program_id": program_id, "reason": reason})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest
        )
        if replay is not None:
            return await self._replay_program(replay)
        program = await self._locked_program(program_id)
        if program.status == ReferralProgramStatus.DISABLED:
            raise ConflictError("REFERRAL_PROGRAM_DISABLED", "Program is already disabled")
        before = self.program_state(program)
        program.status = ReferralProgramStatus.DISABLED
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="referral_program", resource_id=program.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="referral_program", entity_id=program.id,
            action="REFERRAL_PROGRAM_DISABLED", before_state=before,
            after_state=self.program_state(program), reason=reason,
        )
        await self.session.flush()
        response = self.program_snapshot(program)
        record.response_payload = response
        await self._finish(commit=commit)
        return ReferralMutationResult(program, response, False)

    async def void_reward(
        self, *, reward_id: UUID, actor_user_id: UUID, reason: str,
        idempotency_key: str, commit: bool,
    ) -> dict[str, object]:
        await self._require_admin(actor_user_id)
        reason = self._text(reason, field="reason", maximum=500)
        scope = f"admin:{actor_user_id}:referral-reward.void"
        digest = fingerprint({"reward_id": reward_id, "reason": reason})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest
        )
        if replay is not None:
            if not isinstance(replay.response_payload, dict):
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior reward review cannot be recovered")
            return dict(replay.response_payload)
        reward = await self.session.scalar(
            select(ReferralReward).where(ReferralReward.id == reward_id).with_for_update()
        )
        if reward is None:
            raise ValidationError("UNKNOWN_REFERRAL_REWARD", "Referral reward does not exist")
        if reward.status != ReferralRewardStatus.PENDING_REVIEW:
            raise ConflictError("REFERRAL_REWARD_NOT_PENDING", "Reward is not pending review")
        before = self.reward_state(reward)
        reward.status = ReferralRewardStatus.VOIDED
        reward.voided_by_user_id = actor_user_id
        reward.voided_at = datetime.now(timezone.utc)
        reward.void_reason = reason
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="referral_reward", resource_id=reward.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="referral_reward", entity_id=reward.id,
            action="REFERRAL_REWARD_VOIDED", before_state=before,
            after_state=self.reward_state(reward), reason=reason,
        )
        await self.session.flush()
        response = self.reward_snapshot(reward)
        record.response_payload = response
        await self._finish(commit=commit)
        return response

    async def claim(
        self,
        *,
        referred_user_id: UUID,
        referral_code: str,
        idempotency_key: str,
        commit: bool,
    ) -> ReferralMutationResult:
        """Bind an active program and another user's code before checkout."""

        await self._require_active_user(referred_user_id, lock=True)
        code = self._code(referral_code)
        scope = f"user:{referred_user_id}:referral.claim"
        request_fingerprint = fingerprint({"action": "claim", "referral_code": code})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_referral(replay)
        await lock_key_for_transaction(self.session, "referral-code", code)
        profile = await self.session.scalar(
            select(ReferralProfile)
            .where(ReferralProfile.code == code)
            .with_for_update()
        )
        if profile is None or not profile.is_active:
            raise ValidationError("UNKNOWN_REFERRAL_CODE", "Referral code is not active")
        if profile.user_id == referred_user_id:
            raise ConflictError("SELF_REFERRAL_FORBIDDEN", "You cannot use your own referral code")
        existing = await self.session.scalar(
            select(Referral).where(Referral.referred_user_id == referred_user_id).with_for_update()
        )
        if existing is not None:
            raise ConflictError("REFERRAL_ALREADY_CLAIMED", "A referral was already claimed for this user")
        prior_settlement = await self.session.scalar(
            select(Order.id)
            .where(
                Order.buyer_user_id == referred_user_id,
                Order.settlement_reference_id.is_not(None),
            )
            .limit(1)
        )
        if prior_settlement is not None:
            raise ConflictError("REFERRAL_TOO_LATE", "Referral must be claimed before the first settlement")
        program = await self._active_program(now=datetime.now(timezone.utc))
        referral = Referral(
            id=uuid4(),
            program_id=program.id,
            referrer_user_id=profile.user_id,
            referred_user_id=referred_user_id,
            referral_code_snapshot=profile.code,
            program_snapshot=self.program_policy_snapshot(program),
            status=ReferralStatus.PENDING,
            claimed_at=datetime.now(timezone.utc),
        )
        self.session.add(referral)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="referral",
            resource_id=referral.id,
            status_code=201,
        )
        self.audit.record(
            actor_user_id=referred_user_id,
            entity_type="referral",
            entity_id=referral.id,
            action="REFERRAL_CLAIMED",
            before_state=None,
            after_state=self.referral_state(referral),
        )
        await self.session.flush()
        response = self.referral_snapshot(referral)
        record.response_payload = response
        await self._finish(commit=commit)
        return ReferralMutationResult(referral, response, False)

    async def qualify_settled_order(
        self,
        *,
        order: Order,
        settlement_reference_id: UUID,
        actor_user_id: UUID | None,
    ) -> list[ReferralReward]:
        """Create pending review rewards once for the first eligible settlement."""

        self._assert_settled_order(order, settlement_reference_id)
        referral = await self.session.scalar(
            select(Referral)
            .where(
                Referral.referred_user_id == order.buyer_user_id,
                Referral.status == ReferralStatus.PENDING,
            )
            .with_for_update()
        )
        if referral is None:
            return []
        policy = referral.program_snapshot
        minimum = self._snapshot_paise(policy, "minimum_order_paise")
        if order.total_paise < minimum:
            return []
        prior_settlement = await self.session.scalar(
            select(Order.id)
            .where(
                Order.buyer_user_id == order.buyer_user_id,
                Order.id != order.id,
                Order.settlement_reference_id.is_not(None),
            )
            .limit(1)
        )
        if prior_settlement is not None:
            return []
        referrer_amount = self._snapshot_paise(policy, "referrer_reward_paise")
        referred_amount = self._snapshot_paise(policy, "referred_reward_paise")
        existing = list(
            await self.session.scalars(
                select(ReferralReward)
                .where(ReferralReward.referral_id == referral.id)
                .with_for_update()
            )
        )
        if existing:
            raise InvariantViolationError("Pending referral has pre-existing reward evidence")
        before = self.referral_state(referral)
        now = datetime.now(timezone.utc)
        referral.status = ReferralStatus.QUALIFIED
        referral.qualifying_order_id = order.id
        referral.qualified_at = now
        rewards = [
            ReferralReward(
                id=uuid4(),
                referral_id=referral.id,
                qualifying_order_id=order.id,
                recipient_user_id=referral.referrer_user_id,
                recipient=ReferralRewardRecipient.REFERRER,
                amount_paise=referrer_amount,
                currency="INR",
                status=ReferralRewardStatus.PENDING_REVIEW,
                created_from_settlement_id=settlement_reference_id,
            ),
            ReferralReward(
                id=uuid4(),
                referral_id=referral.id,
                qualifying_order_id=order.id,
                recipient_user_id=referral.referred_user_id,
                recipient=ReferralRewardRecipient.REFERRED,
                amount_paise=referred_amount,
                currency="INR",
                status=ReferralRewardStatus.PENDING_REVIEW,
                created_from_settlement_id=settlement_reference_id,
            ),
        ]
        self.session.add_all(rewards)
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="referral",
            entity_id=referral.id,
            action="REFERRAL_QUALIFIED_BY_SETTLEMENT",
            before_state=before,
            after_state=self.referral_state(referral),
        )
        for reward in rewards:
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="referral_reward",
                entity_id=reward.id,
                action="REFERRAL_REWARD_PENDING_REVIEW",
                before_state=None,
                after_state=self.reward_state(reward),
            )
        return rewards

    async def list_programs(self, *, limit: int) -> list[ReferralProgram]:
        return list(
            await self.session.scalars(
                select(ReferralProgram)
                .order_by(ReferralProgram.created_at.desc(), ReferralProgram.id)
                .limit(self._limit(limit))
            )
        )

    async def list_rewards(self, *, limit: int) -> list[ReferralReward]:
        return list(
            await self.session.scalars(
                select(ReferralReward)
                .order_by(ReferralReward.created_at.desc(), ReferralReward.id)
                .limit(self._limit(limit))
            )
        )

    async def get_profile_for_user(self, *, user_id: UUID) -> ReferralProfile | None:
        await self._require_active_user(user_id, lock=False)
        return await self.session.scalar(
            select(ReferralProfile).where(ReferralProfile.user_id == user_id)
        )

    async def get_referral_for_user(self, *, user_id: UUID) -> Referral | None:
        await self._require_active_user(user_id, lock=False)
        return await self.session.scalar(
            select(Referral).where(Referral.referred_user_id == user_id)
        )

    async def _new_profile(self, user_id: UUID) -> ReferralProfile:
        for _ in range(10):
            code = f"REF-{uuid4().hex[:12].upper()}"
            await lock_key_for_transaction(self.session, "referral-code", code)
            conflict = await self.session.scalar(
                select(ReferralProfile.id).where(ReferralProfile.code == code).with_for_update()
            )
            if conflict is None:
                return ReferralProfile(id=uuid4(), user_id=user_id, code=code, is_active=True)
        raise ConflictError("REFERRAL_CODE_GENERATION_FAILED", "Could not reserve a unique referral code")

    async def _active_program(self, *, now: datetime) -> ReferralProgram:
        program = await self.session.scalar(
            select(ReferralProgram)
            .where(ReferralProgram.status == ReferralProgramStatus.ACTIVE)
            .with_for_update()
        )
        if program is None:
            raise ConflictError("REFERRAL_PROGRAM_UNAVAILABLE", "No active referral program is available")
        self._assert_program_window(program, now=now)
        return program

    async def _locked_program(self, program_id: UUID) -> ReferralProgram:
        program = await self.session.scalar(
            select(ReferralProgram).where(ReferralProgram.id == program_id).with_for_update()
        )
        if program is None:
            raise ValidationError("UNKNOWN_REFERRAL_PROGRAM", "Referral program does not exist")
        return program

    async def _require_active_user(self, user_id: UUID, *, lock: bool) -> User:
        statement = select(User).options(selectinload(User.roles)).where(User.id == user_id)
        if lock:
            statement = statement.with_for_update()
        user = await self.session.scalar(statement)
        if user is None or not user.is_active:
            raise AuthorizationError("An active user account is required")
        return user

    async def _require_admin(self, user_id: UUID) -> User:
        user = await self._require_active_user(user_id, lock=True)
        if RoleName.ADMIN.value not in {role.name for role in user.roles}:
            raise AuthorizationError("Administrator role is required")
        return user

    async def _replay_program(self, record: IdempotencyRecord) -> ReferralMutationResult:
        program = await self._record_resource(record, ReferralProgram, "referral program")
        return ReferralMutationResult(program, dict(record.response_payload), True)

    async def _replay_profile(self, record: IdempotencyRecord) -> ReferralMutationResult:
        profile = await self._record_resource(record, ReferralProfile, "referral profile")
        return ReferralMutationResult(profile, dict(record.response_payload), True)

    async def _replay_referral(self, record: IdempotencyRecord) -> ReferralMutationResult:
        referral = await self._record_resource(record, Referral, "referral")
        return ReferralMutationResult(referral, dict(record.response_payload), True)

    async def _record_resource(self, record: IdempotencyRecord, model, label: str):
        if record.resource_id is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", f"Prior {label} mutation cannot be recovered")
        resource = await self.session.get(model, record.resource_id)
        if resource is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", f"Prior {label} no longer exists")
        return resource

    @classmethod
    def _program_values(
        cls,
        *,
        name: object,
        description: object,
        referrer_reward_paise: object,
        referred_reward_paise: object,
        minimum_order_paise: object,
        starts_at: object,
        ends_at: object,
    ) -> dict[str, object]:
        start = cls._optional_aware(starts_at, field="starts_at")
        end = cls._optional_aware(ends_at, field="ends_at")
        if start is not None and end is not None and end <= start:
            raise ValidationError("INVALID_REFERRAL_WINDOW", "ends_at must be after starts_at")
        return {
            "name": cls._text(name, field="name", maximum=120),
            "description": cls._optional_text(description, field="description", maximum=500),
            "referrer_reward_paise": require_paise(
                referrer_reward_paise, field="referrer_reward_paise"
            ),
            "referred_reward_paise": require_paise(
                referred_reward_paise, field="referred_reward_paise"
            ),
            "minimum_order_paise": require_paise(minimum_order_paise, field="minimum_order_paise"),
            "starts_at": start,
            "ends_at": end,
        }

    @staticmethod
    def _assert_settled_order(order: Order, settlement_reference_id: UUID) -> None:
        if (
            order.status not in {OrderStatus.PAID, OrderStatus.FULFILLED}
            or order.settlement_reference_id != settlement_reference_id
            or order.settled_at is None
        ):
            raise ConflictError("ORDER_NOT_SETTLED", "Referral qualification requires a durable settlement")

    @staticmethod
    def _assert_program_window(program: ReferralProgram, *, now: datetime) -> None:
        if program.starts_at is not None and ReferralService._utc(program.starts_at) > now:
            raise ConflictError("REFERRAL_PROGRAM_NOT_STARTED", "Referral program is not active yet")
        if program.ends_at is not None and ReferralService._utc(program.ends_at) <= now:
            raise ConflictError("REFERRAL_PROGRAM_EXPIRED", "Referral program has expired")

    @staticmethod
    def _snapshot_paise(snapshot: object, name: str) -> int:
        if not isinstance(snapshot, dict):
            raise InvariantViolationError("Referral program snapshot is invalid")
        value = snapshot.get(name)
        try:
            return require_paise(value, field=name)
        except ValidationError as error:
            raise InvariantViolationError("Referral program snapshot is invalid") from error

    @staticmethod
    def _code(value: object) -> str:
        if not isinstance(value, str):
            raise ValidationError("INVALID_REFERRAL_CODE", "Referral code must be a string")
        code = value.strip().upper()
        if not _CODE.fullmatch(code):
            raise ValidationError("INVALID_REFERRAL_CODE", "Referral code has an invalid format")
        return code

    @staticmethod
    def _text(value: object, *, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ValidationError("INVALID_REFERRAL", f"{field} must be a non-empty string up to {maximum} characters")
        return value.strip()

    @classmethod
    def _optional_text(cls, value: object, *, field: str, maximum: int) -> str | None:
        return None if value is None else cls._text(value, field=field, maximum=maximum)

    @staticmethod
    def _optional_aware(value: object, *, field: str) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("INVALID_REFERRAL_WINDOW", f"{field} must include a timezone")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _limit(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 1_000:
            raise ValidationError("INVALID_LIMIT", "limit must be an integer between 1 and 1000")
        return value

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _datetime(value: datetime | None) -> str | None:
        return None if value is None else ReferralService._utc(value).isoformat()

    @classmethod
    def program_policy_snapshot(cls, program: ReferralProgram) -> dict[str, object]:
        return {
            "program_id": str(program.id),
            "referrer_reward_paise": program.referrer_reward_paise,
            "referred_reward_paise": program.referred_reward_paise,
            "minimum_order_paise": program.minimum_order_paise,
            "starts_at": cls._datetime(program.starts_at),
            "ends_at": cls._datetime(program.ends_at),
        }

    @classmethod
    def profile_state(cls, profile: ReferralProfile) -> dict[str, object]:
        return {"id": profile.id, "user_id": profile.user_id, "code": profile.code, "is_active": profile.is_active}

    @classmethod
    def program_state(cls, program: ReferralProgram) -> dict[str, object]:
        return {
            "id": program.id,
            "name": program.name,
            "description": program.description,
            "referrer_reward_paise": program.referrer_reward_paise,
            "referred_reward_paise": program.referred_reward_paise,
            "minimum_order_paise": program.minimum_order_paise,
            "starts_at": cls._datetime(program.starts_at),
            "ends_at": cls._datetime(program.ends_at),
            "status": program.status,
            "created_by_user_id": program.created_by_user_id,
            "activated_by_user_id": program.activated_by_user_id,
            "activated_at": cls._datetime(program.activated_at),
        }

    @classmethod
    def referral_state(cls, referral: Referral) -> dict[str, object]:
        return {
            "id": referral.id,
            "program_id": referral.program_id,
            "referrer_user_id": referral.referrer_user_id,
            "referred_user_id": referral.referred_user_id,
            "referral_code_snapshot": referral.referral_code_snapshot,
            "program_snapshot": referral.program_snapshot,
            "status": referral.status,
            "qualifying_order_id": referral.qualifying_order_id,
            "claimed_at": cls._datetime(referral.claimed_at),
            "qualified_at": cls._datetime(referral.qualified_at),
            "voided_at": cls._datetime(referral.voided_at),
            "void_reason": referral.void_reason,
        }

    @classmethod
    def reward_state(cls, reward: ReferralReward) -> dict[str, object]:
        return {
            "id": reward.id,
            "referral_id": reward.referral_id,
            "qualifying_order_id": reward.qualifying_order_id,
            "recipient_user_id": reward.recipient_user_id,
            "recipient": reward.recipient,
            "amount_paise": reward.amount_paise,
            "currency": reward.currency,
            "status": reward.status,
            "created_from_settlement_id": reward.created_from_settlement_id,
            "voided_at": cls._datetime(reward.voided_at),
            "void_reason": reward.void_reason,
        }

    @classmethod
    def profile_snapshot(cls, profile: ReferralProfile) -> dict[str, object]:
        return canonical_payload(cls.profile_state(profile))

    @classmethod
    def program_snapshot(cls, program: ReferralProgram) -> dict[str, object]:
        return canonical_payload(cls.program_state(program))

    @classmethod
    def referral_snapshot(cls, referral: Referral) -> dict[str, object]:
        return canonical_payload(cls.referral_state(referral))

    @classmethod
    def reward_snapshot(cls, reward: ReferralReward) -> dict[str, object]:
        return canonical_payload(cls.reward_state(reward))

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()
