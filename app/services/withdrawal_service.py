"""P2P withdrawal eligibility snapshots, named holds and safe cancellation."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_paise
from app.core.permissions import RoleName
from app.core.state_machine import WITHDRAWAL_TRANSITIONS, require_transition
from app.exceptions import AuthorizationError, ConflictError, ValidationError
from app.models.p2p_match import P2PMatch
from app.models.user import IdempotencyRecord, User
from app.models.wallet import Wallet
from app.models.withdrawal import PaymentDestination, WithdrawalRequest, WithdrawalStatus
from app.repositories.wallet_repository import get_wallet_by_id, get_wallet_by_user_id
from app.repositories.withdrawal_repository import (
    get_unresolved_withdrawal_for_user,
    get_verified_destination_for_owner,
    get_withdrawal,
)
from app.services.wallet_service import WalletService
from app.services.p2p_risk_service import P2PRiskService


P2P_WITHDRAWAL_REFERENCE_TYPE = "p2p_withdrawal"


@dataclass(frozen=True, slots=True)
class WithdrawalMutationResult:
    withdrawal: WithdrawalRequest
    response_payload: dict[str, object]
    replayed: bool


class WithdrawalService:
    """Mutate P2P withdrawals only while their receiver wallet is locked."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)
        self.wallets = WalletService(session)
        self.risk = P2PRiskService(session)

    async def create(
        self,
        *,
        actor_user_id: UUID,
        amount_paise: int,
        payment_destination_id: UUID,
        idempotency_key: str,
        commit: bool,
    ) -> WithdrawalMutationResult:
        """Snapshot eligibility and create exactly one named wallet hold atomically."""

        amount = require_paise(amount_paise)
        await self.risk.assert_eligible(actor_user_id)
        if not isinstance(payment_destination_id, UUID):
            raise ValidationError("INVALID_DESTINATION", "payment_destination_id must be a UUID")
        scope = f"user:{actor_user_id}:withdrawal.create"
        request_fingerprint = fingerprint(
            {"amount_paise": amount, "payment_destination_id": payment_destination_id}
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)

        existing = await get_unresolved_withdrawal_for_user(
            self.session, user_id=actor_user_id, for_update=True
        )
        if existing is not None:
            raise ConflictError(
                "ACTIVE_WITHDRAWAL_EXISTS",
                f"Withdrawal {existing.id} is still unresolved and owns the active hold",
            )
        # An existing request is locked before its wallet everywhere in the
        # P2P flow.  For a new request, the initial empty lookup is followed
        # by a wallet lock and a second lookup so two concurrent creates still
        # serialize safely without reversing that lock order.
        wallet = await get_wallet_by_user_id(self.session, actor_user_id, for_update=True)
        if wallet is None:
            raise ValidationError("UNKNOWN_WALLET", "An INR wallet is required before requesting a withdrawal")
        existing = await get_unresolved_withdrawal_for_user(
            self.session, user_id=actor_user_id, for_update=True
        )
        if existing is not None:
            raise ConflictError(
                "ACTIVE_WITHDRAWAL_EXISTS",
                f"Withdrawal {existing.id} is still unresolved and owns the active hold",
            )
        destination = await get_verified_destination_for_owner(
            self.session,
            destination_id=payment_destination_id,
            user_id=actor_user_id,
            for_update=True,
        )
        if destination is None:
            raise ValidationError(
                "VERIFIED_DESTINATION_REQUIRED",
                "Select a receiver payment destination that has been verified by the controlled workflow",
            )
        self._validate_destination(destination)
        eligible_balance = wallet.available_paise
        max_amount, rule_snapshot = self._eligible_maximum(eligible_balance)
        settings = get_settings()
        if amount < settings.p2p_min_withdrawal_paise or amount > max_amount:
            raise ValidationError(
                "INSUFFICIENT_ELIGIBLE_BALANCE",
                f"Requested amount is outside the eligible range; maximum is {max_amount} paise",
            )
        withdrawal = WithdrawalRequest(
            id=uuid4(),
            user_id=actor_user_id,
            wallet_id=wallet.id,
            payment_destination_id=destination.id,
            amount_paise=amount,
            currency="INR",
            eligible_balance_snapshot_paise=eligible_balance,
            max_amount_snapshot_paise=max_amount,
            rule_snapshot=rule_snapshot,
            rule_version=settings.p2p_rule_version,
            status=WithdrawalStatus.WAITING_FOR_BUYER,
        )
        self.session.add(withdrawal)
        await self.session.flush()
        held = await self.wallets.hold_available(
            wallet_id=wallet.id,
            actor_user_id=actor_user_id,
            amount_paise=amount,
            idempotency_key=idempotency_key,
            reference_type=P2P_WITHDRAWAL_REFERENCE_TYPE,
            reference_id=withdrawal.id,
            reason="P2P withdrawal request hold",
            commit=False,
        )
        if held.wallet_hold is None:
            raise ConflictError("WALLET_HOLD_INCOMPLETE", "Withdrawal hold did not persist")
        withdrawal.wallet_hold_id = held.wallet_hold.id
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="withdrawal_request",
            resource_id=withdrawal.id,
            status_code=201,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="withdrawal_request",
            entity_id=withdrawal.id,
            action="P2P_WITHDRAWAL_CREATED",
            before_state=None,
            after_state=self._state(withdrawal),
            reason="Eligible balance and rule snapshot stored before the named hold",
        )
        await self.session.flush()
        response = self.snapshot(withdrawal)
        record.response_payload = response
        await self._finish(commit=commit)
        return WithdrawalMutationResult(withdrawal, response, False)

    async def cancel(
        self,
        *,
        withdrawal_id: UUID,
        actor_user_id: UUID,
        idempotency_key: str,
        commit: bool,
    ) -> WithdrawalMutationResult:
        """Release an unexposed hold once; exposed attempts must use reconciliation."""

        initial = await get_withdrawal(self.session, withdrawal_id)
        if initial is None:
            raise ValidationError("UNKNOWN_WITHDRAWAL", "Withdrawal request does not exist")
        if initial.user_id != actor_user_id:
            raise AuthorizationError("You cannot cancel another receiver's withdrawal")
        withdrawal = await get_withdrawal(self.session, withdrawal_id, for_update=True)
        if withdrawal is None:
            raise ValidationError("UNKNOWN_WITHDRAWAL", "Withdrawal request does not exist")
        wallet = await get_wallet_by_id(self.session, withdrawal.wallet_id, for_update=True)
        if wallet is None:
            raise ConflictError("WALLET_INCONSISTENT", "Withdrawal wallet no longer exists")
        scope = f"user:{actor_user_id}:withdrawal.cancel"
        request_fingerprint = fingerprint({"withdrawal_id": withdrawal_id, "action": "cancel"})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        if withdrawal.status != WithdrawalStatus.WAITING_FOR_BUYER:
            raise ConflictError(
                "REVIEW_REQUIRED",
                "Only an unmatched, unexposed withdrawal can be cancelled automatically",
            )
        exposed_match = await self.session.scalar(
            select(P2PMatch.id).where(
                P2PMatch.withdrawal_id == withdrawal.id,
                P2PMatch.instructions_exposed_at.is_not(None),
            )
        )
        if exposed_match is not None:
            raise ConflictError(
                "REVIEW_REQUIRED",
                "This withdrawal has exposed payment instructions and must be reconciled",
            )
        before = self._state(withdrawal)
        await self.wallets.release_locked(
            wallet_id=wallet.id,
            actor_user_id=actor_user_id,
            amount_paise=withdrawal.amount_paise,
            idempotency_key=idempotency_key,
            reference_type=P2P_WITHDRAWAL_REFERENCE_TYPE,
            reference_id=withdrawal.id,
            reason="Unmatched P2P withdrawal cancelled before payment exposure",
            commit=False,
        )
        require_transition(
            current=withdrawal.status,
            target=WithdrawalStatus.CANCELLED,
            transitions=WITHDRAWAL_TRANSITIONS,
            resource="Withdrawal",
        )
        withdrawal.status = WithdrawalStatus.CANCELLED
        withdrawal.cancelled_at = datetime.now(timezone.utc)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="withdrawal_request",
            resource_id=withdrawal.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="withdrawal_request",
            entity_id=withdrawal.id,
            action="P2P_WITHDRAWAL_CANCELLED_AND_HOLD_RELEASED",
            before_state=before,
            after_state=self._state(withdrawal),
            reason="No payment destination had been exposed",
        )
        await self.session.flush()
        response = self.snapshot(withdrawal)
        record.response_payload = response
        await self._finish(commit=commit)
        return WithdrawalMutationResult(withdrawal, response, False)

    async def get_for_actor(self, *, withdrawal_id: UUID, actor_user_id: UUID) -> WithdrawalRequest:
        withdrawal = await get_withdrawal(self.session, withdrawal_id)
        if withdrawal is None:
            raise ValidationError("UNKNOWN_WITHDRAWAL", "Withdrawal request does not exist")
        actor = await self._active_user(actor_user_id)
        if withdrawal.user_id != actor_user_id and RoleName.ADMIN.value not in {role.name for role in actor.roles}:
            raise AuthorizationError("You cannot view another receiver's withdrawal")
        return withdrawal

    async def _active_user(self, user_id: UUID) -> User:
        user = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == user_id)
        )
        if user is None or not user.is_active:
            raise AuthorizationError("An active user account is required")
        return user

    async def _replay(self, record: IdempotencyRecord) -> WithdrawalMutationResult:
        if record.resource_id is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior withdrawal mutation cannot be recovered")
        withdrawal = await get_withdrawal(self.session, record.resource_id)
        if withdrawal is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior withdrawal no longer exists")
        return WithdrawalMutationResult(withdrawal, dict(record.response_payload), True)

    @staticmethod
    def _validate_destination(destination: PaymentDestination) -> None:
        if not isinstance(destination.destination_data, dict) or not destination.destination_data:
            raise ConflictError("DESTINATION_INCONSISTENT", "Verified destination lacks recipient data")

    @staticmethod
    def _eligible_maximum(available_paise: int) -> tuple[int, dict[str, object]]:
        if available_paise < 0:
            raise ConflictError("WALLET_INCONSISTENT", "Available wallet balance cannot be negative")
        settings = get_settings()
        maximum = (available_paise * settings.p2p_withdrawal_percentage_bps) // 10_000
        if settings.p2p_threshold_override_enabled and available_paise <= settings.p2p_threshold_paise:
            maximum = available_paise
        maximum = min(maximum, settings.p2p_max_withdrawal_paise)
        return maximum, canonical_payload(
            {
                "percentage_bps": settings.p2p_withdrawal_percentage_bps,
                "threshold_override_enabled": settings.p2p_threshold_override_enabled,
                "threshold_paise": settings.p2p_threshold_paise,
                "configured_min_withdrawal_paise": settings.p2p_min_withdrawal_paise,
                "configured_max_withdrawal_paise": settings.p2p_max_withdrawal_paise,
                "eligible_available_before_hold_paise": available_paise,
            }
        )

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()

    @staticmethod
    def _aware(value: datetime | None) -> str | None:
        if value is None:
            return None
        return (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).isoformat()

    @classmethod
    def _state(cls, withdrawal: WithdrawalRequest) -> dict[str, object]:
        return {
            "id": withdrawal.id,
            "user_id": withdrawal.user_id,
            "wallet_id": withdrawal.wallet_id,
            "payment_destination_id": withdrawal.payment_destination_id,
            "wallet_hold_id": withdrawal.wallet_hold_id,
            "amount_paise": withdrawal.amount_paise,
            "currency": withdrawal.currency,
            "eligible_balance_snapshot_paise": withdrawal.eligible_balance_snapshot_paise,
            "max_amount_snapshot_paise": withdrawal.max_amount_snapshot_paise,
            "rule_snapshot": withdrawal.rule_snapshot,
            "rule_version": withdrawal.rule_version,
            "status": withdrawal.status,
            "matched_at": cls._aware(withdrawal.matched_at),
            "completed_at": cls._aware(withdrawal.completed_at),
            "cancelled_at": cls._aware(withdrawal.cancelled_at),
        }

    @classmethod
    def snapshot(cls, withdrawal: WithdrawalRequest) -> dict[str, object]:
        return canonical_payload(cls._state(withdrawal))
