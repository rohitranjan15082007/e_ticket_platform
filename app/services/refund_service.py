"""Safe creation of post-settlement P2P refund obligations.

This module deliberately does not initiate or verify a payout.  It records
the evidence a later funded workflow must use, while retaining the original
settlement history and receiver wallet outcome unchanged.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_inr_currency, require_paise
from app.core.state_machine import ORDER_TRANSITIONS, P2P_MATCH_TRANSITIONS, require_transition
from app.exceptions import ConflictError, ValidationError
from app.models.order import OrderStatus
from app.models.p2p_match import P2PMatchStatus, P2PRefund, P2PRefundStatus
from app.models.user import IdempotencyRecord
from app.repositories.payment_repository import (
    get_refund_for_match,
    get_settlement_for_match,
    get_verified_reference_for_match,
)
from app.services.order_service import OrderService
from app.services.p2p_service import P2PService


@dataclass(frozen=True, slots=True)
class RefundCaseMutationResult:
    refund: P2PRefund
    response_payload: dict[str, object]
    replayed: bool


class RefundService:
    """Record a controlled refund case without moving money or fulfilling it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)
        self.p2p = P2PService(session)

    async def create_pending_case(
        self,
        *,
        match_id: UUID,
        actor_user_id: UUID,
        amount_paise: int,
        currency: str,
        reason: str,
        liable_party: str,
        funding_source_reference: str,
        destination_validation_reference: str,
        executor_reference: str,
        idempotency_key: str,
        commit: bool,
    ) -> RefundCaseMutationResult:
        """Create one admin-owned evidence case for an already-settled match.

        No caller can supply a payout destination, payout reference, or target
        status.  A separately authorized future workflow must fund and verify
        any payout before this case may become paid.
        """

        await self.p2p._require_admin(actor_user_id)
        if not isinstance(match_id, UUID):
            raise ValidationError("INVALID_MATCH", "match_id must be a UUID")
        amount = require_paise(amount_paise)
        currency = require_inr_currency(currency)
        reason = self._text(reason, field="reason", limit=500)
        liable_party = self._text(liable_party, field="liable_party", limit=100)
        funding_source_reference = self._text(
            funding_source_reference, field="funding_source_reference", limit=255
        )
        destination_validation_reference = self._text(
            destination_validation_reference, field="destination_validation_reference", limit=255
        )
        executor_reference = self._text(executor_reference, field="executor_reference", limit=255)

        context = await self.p2p._locked_match_context(match_id)
        scope = f"admin:{actor_user_id}:p2p.refund-case"
        request_fingerprint = fingerprint(
            {
                "match_id": match_id,
                "amount_paise": amount,
                "currency": currency,
                "reason": reason,
                "liable_party": liable_party,
                "funding_source_reference": funding_source_reference,
                "destination_validation_reference": destination_validation_reference,
                "executor_reference": executor_reference,
            }
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay(replay)
        existing = await get_refund_for_match(self.session, context.match.id, for_update=True)
        if existing is not None:
            raise ConflictError(
                "REFUND_CASE_EXISTS", "A refund case already exists for this P2P payment attempt"
            )
        settlement = await get_settlement_for_match(self.session, context.match.id, for_update=True)
        verified_reference = await get_verified_reference_for_match(
            self.session, context.match.id, for_update=True
        )
        if settlement is None or verified_reference is None:
            raise ConflictError(
                "REVIEW_REQUIRED",
                "A refund case requires the original settled payment and verified receipt reference",
            )
        if context.match.status != P2PMatchStatus.SETTLED:
            raise ConflictError("INVALID_TRANSITION", "Only a settled P2P attempt can enter refund review")
        if context.order.status not in {OrderStatus.PAID, OrderStatus.FULFILLED}:
            raise ConflictError("ORDER_NOT_SETTLED", "The related order is not in a refundable settled state")
        if currency != settlement.currency or amount > settlement.amount_paise:
            raise ValidationError(
                "REFUND_AMOUNT_INVALID", "Refund amount must be positive INR and no greater than the settlement"
            )

        before_match = self.p2p.match_state(context.match)
        before_order = OrderService._order_state(context.order)
        refund = P2PRefund(
            id=uuid4(),
            match_id=context.match.id,
            settlement_id=settlement.id,
            verified_payment_reference_id=verified_reference.id,
            amount_paise=amount,
            currency=currency,
            reason=reason,
            liable_party=liable_party,
            funding_source_reference=funding_source_reference,
            destination_validation_reference=destination_validation_reference,
            executor_reference=executor_reference,
            created_by_user_id=actor_user_id,
            status=P2PRefundStatus.PENDING_EVIDENCE,
        )
        self.session.add(refund)
        require_transition(
            current=context.match.status,
            target=P2PMatchStatus.REFUND_PENDING,
            transitions=P2P_MATCH_TRANSITIONS,
            resource="P2P match",
        )
        context.match.status = P2PMatchStatus.REFUND_PENDING
        require_transition(
            current=context.order.status,
            target=OrderStatus.REFUND_PENDING,
            transitions=ORDER_TRANSITIONS,
            resource="Order",
        )
        context.order.status = OrderStatus.REFUND_PENDING
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_refund",
            resource_id=refund.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_refund",
            entity_id=refund.id,
            action="P2P_REFUND_CASE_CREATED_AWAITING_PAYOUT_EVIDENCE",
            before_state=None,
            after_state=self._refund_state(refund),
            reason=reason,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_match",
            entity_id=context.match.id,
            action="P2P_MATCH_ENTERED_REFUND_REVIEW",
            before_state=before_match,
            after_state=self.p2p.match_state(context.match),
            reason=reason,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="order",
            entity_id=context.order.id,
            action="P2P_ORDER_ENTERED_REFUND_REVIEW",
            before_state=before_order,
            after_state=OrderService._order_state(context.order),
            reason=reason,
        )
        await self.session.flush()
        response = self.snapshot(refund)
        record.response_payload = response
        await self._finish(commit=commit)
        return RefundCaseMutationResult(refund, response, False)

    async def _replay(self, record: IdempotencyRecord) -> RefundCaseMutationResult:
        if record.resource_id is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior refund case cannot be recovered")
        refund = await self.session.get(P2PRefund, record.resource_id)
        if refund is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior refund case no longer exists")
        return RefundCaseMutationResult(refund, dict(record.response_payload), True)

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()

    @staticmethod
    def _text(value: object, *, field: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise ValidationError("INVALID_REFERENCE", f"{field} must be a non-empty string up to {limit} characters")
        return value.strip()

    @staticmethod
    def _aware(value: datetime | None) -> str | None:
        if value is None:
            return None
        return (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)).isoformat()

    @classmethod
    def _refund_state(cls, refund: P2PRefund) -> dict[str, object]:
        return {
            "id": refund.id,
            "match_id": refund.match_id,
            "settlement_id": refund.settlement_id,
            "verified_payment_reference_id": refund.verified_payment_reference_id,
            "amount_paise": refund.amount_paise,
            "currency": refund.currency,
            "reason": refund.reason,
            "liable_party": refund.liable_party,
            "funding_source_reference": refund.funding_source_reference,
            "destination_validation_reference": refund.destination_validation_reference,
            "executor_reference": refund.executor_reference,
            "status": refund.status,
            "payout_reference": refund.payout_reference,
            "payout_verified_at": cls._aware(refund.payout_verified_at),
        }

    @classmethod
    def snapshot(cls, refund: P2PRefund) -> dict[str, object]:
        return canonical_payload(cls._refund_state(refund))
