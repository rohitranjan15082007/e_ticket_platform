"""Cashback campaign configuration and settlement-keyed pending rewards."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_paise
from app.core.permissions import RoleName
from app.core.transaction_locks import lock_key_for_transaction
from app.exceptions import AuthorizationError, ConflictError, ValidationError
from app.models.cashback import CashbackCampaign, CashbackCampaignStatus, CashbackReward, CashbackRewardStatus, CashbackRewardType
from app.models.order import Order, OrderStatus
from app.models.user import User


class CashbackService:
    """Pending rewards do not credit wallets or debit marketing funds."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)

    async def create_campaign(
        self, *, actor_user_id: UUID, code: str, name: str, description: str | None,
        reward_type: CashbackRewardType, fixed_reward_paise: int | None,
        percentage_bps: int | None, max_reward_paise: int | None,
        minimum_order_paise: int, starts_at: datetime | None, ends_at: datetime | None,
        idempotency_key: str, commit: bool,
    ) -> tuple[CashbackCampaign, dict[str, object], bool]:
        await self._require_admin(actor_user_id)
        code = self._code(code)
        name = self._text(name, 120)
        description = self._text(description, 500) if description is not None else None
        minimum_order_paise = require_paise(minimum_order_paise, field="minimum_order_paise")
        start, end = self._aware(starts_at), self._aware(ends_at)
        if start is not None and end is not None and end <= start:
            raise ValidationError("INVALID_CASHBACK_WINDOW", "ends_at must be after starts_at")
        try:
            reward_type = CashbackRewardType(reward_type)
        except (TypeError, ValueError) as error:
            raise ValidationError("INVALID_CASHBACK_TYPE", "Unknown cashback reward type") from error
        if reward_type == CashbackRewardType.FIXED_PAISE:
            fixed_reward_paise = require_paise(fixed_reward_paise, field="fixed_reward_paise")
            if percentage_bps is not None or max_reward_paise is not None:
                raise ValidationError("INVALID_CASHBACK_RULE", "Fixed reward cannot have percentage fields")
        elif (
            fixed_reward_paise is not None or isinstance(percentage_bps, bool)
            or not isinstance(percentage_bps, int) or not 0 < percentage_bps <= 10_000
        ):
            raise ValidationError("INVALID_CASHBACK_RULE", "Percentage reward requires valid basis points")
        if max_reward_paise is not None:
            max_reward_paise = require_paise(max_reward_paise, field="max_reward_paise")
        values = dict(
            code=code, name=name, description=description, reward_type=reward_type,
            fixed_reward_paise=fixed_reward_paise, percentage_bps=percentage_bps,
            max_reward_paise=max_reward_paise, minimum_order_paise=minimum_order_paise,
            starts_at=start, ends_at=end,
        )
        scope = f"admin:{actor_user_id}:cashback-campaign.create"
        digest = fingerprint({"action": "create", **values})
        replay = await self.idempotency.get_replay(actor_scope=scope, key=idempotency_key, request_fingerprint=digest)
        if replay is not None:
            return await self._replay(replay)
        await lock_key_for_transaction(self.session, "cashback-code", code)
        if await self.session.scalar(select(CashbackCampaign.id).where(CashbackCampaign.code == code)):
            raise ConflictError("CASHBACK_CODE_EXISTS", "Campaign code already exists")
        campaign = CashbackCampaign(id=uuid4(), **values, status=CashbackCampaignStatus.DRAFT, created_by_user_id=actor_user_id)
        self.session.add(campaign)
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="cashback_campaign", resource_id=campaign.id, status_code=201,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="cashback_campaign", entity_id=campaign.id,
            action="CASHBACK_CAMPAIGN_CREATED", before_state=None, after_state=self.snapshot(campaign),
        )
        await self.session.flush()
        payload = self.snapshot(campaign)
        record.response_payload = payload
        if commit:
            await self.session.commit()
        return campaign, payload, False

    async def activate_campaign(
        self, *, campaign_id: UUID, actor_user_id: UUID, idempotency_key: str, commit: bool
    ) -> tuple[CashbackCampaign, dict[str, object], bool]:
        await self._require_admin(actor_user_id)
        scope = f"admin:{actor_user_id}:cashback-campaign.activate"
        digest = fingerprint({"action": "activate", "campaign_id": campaign_id})
        replay = await self.idempotency.get_replay(actor_scope=scope, key=idempotency_key, request_fingerprint=digest)
        if replay is not None:
            return await self._replay(replay)
        await lock_key_for_transaction(self.session, "cashback-active")
        campaign = await self.session.scalar(
            select(CashbackCampaign).where(CashbackCampaign.id == campaign_id).with_for_update()
        )
        if campaign is None:
            raise ValidationError("UNKNOWN_CASHBACK_CAMPAIGN", "Campaign does not exist")
        if campaign.status == CashbackCampaignStatus.DISABLED:
            raise ConflictError("CASHBACK_CAMPAIGN_DISABLED", "Disabled campaign cannot be activated")
        now = datetime.now(timezone.utc)
        if not self._in_window(campaign, now):
            raise ConflictError("CASHBACK_CAMPAIGN_OUTSIDE_WINDOW", "Campaign is outside its time window")
        active = await self.session.scalar(
            select(CashbackCampaign).where(CashbackCampaign.status == CashbackCampaignStatus.ACTIVE).with_for_update()
        )
        if active is not None and active.id != campaign.id:
            raise ConflictError("CASHBACK_CAMPAIGN_ALREADY_ACTIVE", "Disable the active campaign first")
        before = self.snapshot(campaign)
        campaign.status = CashbackCampaignStatus.ACTIVE
        campaign.activated_by_user_id = actor_user_id
        campaign.activated_at = now
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="cashback_campaign", resource_id=campaign.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="cashback_campaign", entity_id=campaign.id,
            action="CASHBACK_CAMPAIGN_ACTIVATED", before_state=before, after_state=self.snapshot(campaign),
        )
        await self.session.flush()
        payload = self.snapshot(campaign)
        record.response_payload = payload
        if commit:
            await self.session.commit()
        return campaign, payload, False

    async def create_pending_reward_for_settled_order(
        self, *, order: Order, settlement_reference_id: UUID, actor_user_id: UUID | None
    ) -> CashbackReward | None:
        if (
            order.status not in {OrderStatus.PAID, OrderStatus.FULFILLED}
            or order.settlement_reference_id != settlement_reference_id or order.settled_at is None
        ):
            raise ConflictError("ORDER_NOT_SETTLED", "Cashback requires a durable settlement")
        campaign = await self.session.scalar(
            select(CashbackCampaign).where(CashbackCampaign.status == CashbackCampaignStatus.ACTIVE).with_for_update()
        )
        if campaign is None or not self._in_window(campaign, self._utc(order.settled_at)):
            return None
        if order.total_paise < campaign.minimum_order_paise:
            return None
        await lock_key_for_transaction(self.session, "cashback-reward", campaign.id, order.id)
        existing = await self.session.scalar(
            select(CashbackReward).where(
                CashbackReward.campaign_id == campaign.id, CashbackReward.order_id == order.id
            ).with_for_update()
        )
        if existing is not None:
            if existing.created_from_settlement_id != settlement_reference_id:
                raise ConflictError("CASHBACK_SETTLEMENT_CONFLICT", "Reward has conflicting settlement evidence")
            return existing
        amount = (
            campaign.fixed_reward_paise if campaign.reward_type == CashbackRewardType.FIXED_PAISE
            else order.total_paise * campaign.percentage_bps // 10_000
        )
        if campaign.max_reward_paise is not None:
            amount = min(amount, campaign.max_reward_paise)
        if amount <= 0:
            return None
        reward = CashbackReward(
            id=uuid4(), campaign_id=campaign.id, order_id=order.id,
            buyer_user_id=order.buyer_user_id, rule_snapshot=self.policy_snapshot(campaign),
            order_base_paise=order.total_paise, amount_paise=amount, currency="INR",
            status=CashbackRewardStatus.PENDING_REVIEW,
            created_from_settlement_id=settlement_reference_id,
        )
        self.session.add(reward)
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="cashback_reward", entity_id=reward.id,
            action="CASHBACK_REWARD_PENDING_REVIEW", before_state=None,
            after_state=self.reward_snapshot(reward),
        )
        return reward

    async def disable_campaign(
        self, *, campaign_id: UUID, actor_user_id: UUID, reason: str,
        idempotency_key: str, commit: bool,
    ) -> dict[str, object]:
        await self._require_admin(actor_user_id)
        reason = self._text(reason, 500)
        scope = f"admin:{actor_user_id}:cashback-campaign.disable"
        digest = fingerprint({"campaign_id": campaign_id, "reason": reason})
        replay = await self.idempotency.get_replay(actor_scope=scope, key=idempotency_key, request_fingerprint=digest)
        if replay is not None:
            if not isinstance(replay.response_payload, dict):
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior campaign review cannot be recovered")
            return dict(replay.response_payload)
        campaign = await self.session.scalar(
            select(CashbackCampaign).where(CashbackCampaign.id == campaign_id).with_for_update()
        )
        if campaign is None:
            raise ValidationError("UNKNOWN_CASHBACK_CAMPAIGN", "Campaign does not exist")
        if campaign.status == CashbackCampaignStatus.DISABLED:
            raise ConflictError("CASHBACK_CAMPAIGN_DISABLED", "Campaign is already disabled")
        before = self.snapshot(campaign)
        campaign.status = CashbackCampaignStatus.DISABLED
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="cashback_campaign", resource_id=campaign.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="cashback_campaign", entity_id=campaign.id,
            action="CASHBACK_CAMPAIGN_DISABLED", before_state=before,
            after_state=self.snapshot(campaign), reason=reason,
        )
        await self.session.flush()
        payload = self.snapshot(campaign)
        record.response_payload = payload
        if commit:
            await self.session.commit()
        return payload

    async def void_reward(
        self, *, reward_id: UUID, actor_user_id: UUID, reason: str,
        idempotency_key: str, commit: bool,
    ) -> dict[str, object]:
        await self._require_admin(actor_user_id)
        reason = self._text(reason, 500)
        scope = f"admin:{actor_user_id}:cashback-reward.void"
        digest = fingerprint({"reward_id": reward_id, "reason": reason})
        replay = await self.idempotency.get_replay(actor_scope=scope, key=idempotency_key, request_fingerprint=digest)
        if replay is not None:
            if not isinstance(replay.response_payload, dict):
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior reward review cannot be recovered")
            return dict(replay.response_payload)
        reward = await self.session.scalar(
            select(CashbackReward).where(CashbackReward.id == reward_id).with_for_update()
        )
        if reward is None:
            raise ValidationError("UNKNOWN_CASHBACK_REWARD", "Cashback reward does not exist")
        if reward.status != CashbackRewardStatus.PENDING_REVIEW:
            raise ConflictError("CASHBACK_REWARD_NOT_PENDING", "Reward is not pending review")
        before = self.reward_snapshot(reward)
        reward.status = CashbackRewardStatus.VOIDED
        reward.voided_by_user_id = actor_user_id
        reward.voided_at = datetime.now(timezone.utc)
        reward.void_reason = reason
        record = self.idempotency.record(
            actor_scope=scope, key=idempotency_key, request_fingerprint=digest,
            resource_type="cashback_reward", resource_id=reward.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id, entity_type="cashback_reward", entity_id=reward.id,
            action="CASHBACK_REWARD_VOIDED", before_state=before,
            after_state=self.reward_snapshot(reward), reason=reason,
        )
        await self.session.flush()
        payload = self.reward_snapshot(reward)
        record.response_payload = payload
        if commit:
            await self.session.commit()
        return payload

    async def list_campaigns(self, *, limit: int) -> list[CashbackCampaign]:
        return list(await self.session.scalars(
            select(CashbackCampaign).order_by(CashbackCampaign.created_at.desc(), CashbackCampaign.id)
            .limit(self._limit(limit))
        ))

    async def list_rewards(self, *, limit: int) -> list[CashbackReward]:
        return list(await self.session.scalars(
            select(CashbackReward).order_by(CashbackReward.created_at.desc(), CashbackReward.id)
            .limit(self._limit(limit))
        ))

    async def _require_admin(self, actor_user_id: UUID) -> None:
        user = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == actor_user_id)
        )
        if user is None or not user.is_active or RoleName.ADMIN.value not in {role.name for role in user.roles}:
            raise AuthorizationError("Administrator role is required")

    async def _replay(self, record) -> tuple[CashbackCampaign, dict[str, object], bool]:
        campaign = await self.session.get(CashbackCampaign, record.resource_id)
        if campaign is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior campaign mutation cannot be recovered")
        return campaign, dict(record.response_payload), True

    @staticmethod
    def _code(value: object) -> str:
        if not isinstance(value, str):
            raise ValidationError("INVALID_CASHBACK_CODE", "Campaign code must be text")
        normalized = value.strip().upper()
        if (
            not 3 <= len(normalized) <= 64 or not normalized[0].isascii()
            or not normalized[0].isalnum()
            or not all((c.isascii() and c.isalnum()) or c in "_-" for c in normalized)
        ):
            raise ValidationError("INVALID_CASHBACK_CODE", "Campaign code is invalid")
        return normalized

    @staticmethod
    def _text(value: object, maximum: int) -> str:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
            raise ValidationError("INVALID_CASHBACK_TEXT", "Campaign text is invalid")
        return value.strip()

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("INVALID_CASHBACK_WINDOW", "Campaign dates must include a timezone")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @classmethod
    def _in_window(cls, campaign: CashbackCampaign, now: datetime) -> bool:
        return (
            (campaign.starts_at is None or cls._utc(campaign.starts_at) <= now)
            and (campaign.ends_at is None or cls._utc(campaign.ends_at) > now)
        )

    @staticmethod
    def _limit(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000:
            raise ValidationError("INVALID_LIMIT", "limit must be 1 through 1000")
        return value

    @classmethod
    def policy_snapshot(cls, campaign: CashbackCampaign) -> dict[str, object]:
        return canonical_payload({
            "code": campaign.code, "reward_type": campaign.reward_type,
            "fixed_reward_paise": campaign.fixed_reward_paise, "percentage_bps": campaign.percentage_bps,
            "max_reward_paise": campaign.max_reward_paise,
            "minimum_order_paise": campaign.minimum_order_paise,
            "starts_at": campaign.starts_at, "ends_at": campaign.ends_at,
        })

    @classmethod
    def snapshot(cls, campaign: CashbackCampaign) -> dict[str, object]:
        return canonical_payload({
            "id": campaign.id, "code": campaign.code, "name": campaign.name,
            "description": campaign.description, "reward_type": campaign.reward_type,
            "fixed_reward_paise": campaign.fixed_reward_paise, "percentage_bps": campaign.percentage_bps,
            "max_reward_paise": campaign.max_reward_paise,
            "minimum_order_paise": campaign.minimum_order_paise,
            "starts_at": campaign.starts_at, "ends_at": campaign.ends_at,
            "status": campaign.status, "created_by_user_id": campaign.created_by_user_id,
            "activated_by_user_id": campaign.activated_by_user_id, "activated_at": campaign.activated_at,
        })

    @classmethod
    def reward_snapshot(cls, reward: CashbackReward) -> dict[str, object]:
        return canonical_payload({
            "id": reward.id, "campaign_id": reward.campaign_id, "order_id": reward.order_id,
            "buyer_user_id": reward.buyer_user_id, "rule_snapshot": reward.rule_snapshot,
            "order_base_paise": reward.order_base_paise, "amount_paise": reward.amount_paise,
            "currency": reward.currency, "status": reward.status,
            "created_from_settlement_id": reward.created_from_settlement_id,
            "voided_at": reward.voided_at, "void_reason": reward.void_reason,
        })
