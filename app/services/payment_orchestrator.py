"""Order-linked Phase 5 payment orchestration.

Adapters authenticate/build instructions only. This service owns durable state,
locks, idempotency, audit records, the balanced external-receipt journal, and
the one-way bridge into ticket delivery. It never treats a screenshot, UTR,
pre-checkout acknowledgement, or an unconfigured adapter as payment success.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Mapping
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.audit import AuditService
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.permissions import RoleName
from app.core.state_machine import ORDER_TRANSITIONS, require_transition
from app.core.transaction_locks import lock_key_for_transaction
from app.exceptions import AuthenticationError, AuthorizationError, ConflictError, ValidationError
from app.models.ledger import PostingDirection
from app.models.order import DeliveryStatus, Order, OrderStatus
from app.models.payment import (
    ManualPaymentDestination,
    ManualPaymentDestinationStatus,
    ManualPaymentProof,
    ManualPaymentProofStatus,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentMethod,
    PaymentOutboxEvent,
    PaymentOutboxEventStatus,
    PaymentProviderEvent,
    PaymentProviderEventStatus,
    PaymentSettlement,
)
from app.models.revenue_allocation import RevenueAllocationSource
from app.models.user import IdempotencyRecord, User
from app.payment_adapters.base import (
    PaymentAdapterConfigurationError,
    PaymentCreation,
    PaymentCreationRequest,
    PaymentEvidenceValidationError,
    PaymentInitiationAvailability,
)
from app.payment_adapters.manual_upi_adapter import ManualUPIAdapter, ManualUPIConfiguration
from app.payment_adapters.telegram_stars_adapter import (
    TELEGRAM_STARS_CURRENCY,
    TelegramStarsAdapter,
    TelegramStarsConfiguration,
)
from app.payment_adapters.white_label_adapter import WhiteLabelAdapter
from app.repositories.generic_payment_repository import (
    get_active_manual_destination,
    get_manual_destination,
    get_manual_proof_by_utr,
    get_manual_proof_for_attempt,
    get_payment_attempt,
    get_payment_attempt_by_merchant_reference,
    get_payment_attempt_by_provider_reference,
    get_payment_attempt_by_telegram_payload,
    get_payment_attempt_for_buyer,
    get_payment_provider_event,
    get_payment_settlement_for_attempt,
    list_manual_proofs_for_review,
)
from app.repositories.order_repository import get_order, get_order_for_buyer
from app.schemas.payment_methods import TelegramUpdate, WhiteLabelWebhookRequest
from app.services.ledger_service import LedgerService, PostingDraft
from app.services.cashback_service import CashbackService
from app.services.coupon_service import CouponService
from app.services.order_service import OrderService
from app.services.referral_service import ReferralService
from app.services.revenue_service import RevenueService


PAYMENT_ORDER_DELIVERY_REQUESTED = "PAYMENT_ORDER_DELIVERY_REQUESTED"
TELEGRAM_STARS_NAMESPACE = "telegram_stars"


@dataclass(slots=True)
class PaymentAttemptMutationResult:
    attempt: PaymentAttempt
    response_payload: dict[str, object]
    replayed: bool


@dataclass(slots=True)
class ManualProofMutationResult:
    attempt: PaymentAttempt
    proof: ManualPaymentProof | None
    response_payload: dict[str, object]
    replayed: bool


@dataclass(slots=True)
class PaymentProviderEventMutationResult:
    event: PaymentProviderEvent
    response_payload: dict[str, object]
    replayed: bool
    pre_checkout_query_id: str | None = None
    pre_checkout_approved: bool | None = None
    pre_checkout_error: str | None = None


@dataclass(slots=True)
class _LockedPaymentContext:
    order: Order
    attempt: PaymentAttempt


class PaymentOrchestrator:
    """Own generic provider, Telegram Stars, and manual-QR payment state."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)
        self.ledger = LedgerService(session)

    async def create_payment_attempt(
        self,
        *,
        order_id: UUID,
        buyer_user_id: UUID,
        method: PaymentMethod,
        telegram_user_id: int | None,
        idempotency_key: str,
        commit: bool,
    ) -> PaymentAttemptMutationResult:
        """Expose frozen payment instructions for an owned, unexposed order."""

        if not isinstance(method, PaymentMethod):
            raise ValidationError("INVALID_PAYMENT_METHOD", "Unsupported payment method")
        order = await get_order_for_buyer(
            self.session, order_id=order_id, buyer_user_id=buyer_user_id, for_update=True
        )
        if order is None:
            raise ValidationError("UNKNOWN_ORDER", "Order does not exist")
        await self._require_active_user(buyer_user_id)
        scope = f"user:{buyer_user_id}:payment.create"
        request_fingerprint = fingerprint(
            {"order_id": order_id, "method": method, "telegram_user_id": telegram_user_id}
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_attempt(replay)
        if order.status != OrderStatus.PENDING_PAYMENT or order.settlement_reference_id is not None:
            raise ConflictError("ORDER_NOT_AVAILABLE", "Order is not available for a new payment method")
        now = datetime.now(timezone.utc)
        if now >= self._as_utc(order.expires_at):
            raise ConflictError("ORDER_EXPIRED", "Order reservation elapsed before payment instructions could be exposed")

        attempt_id = uuid4()
        creation, destination = await self._build_creation(
            method=method,
            order=order,
            buyer_user_id=buyer_user_id,
            attempt_id=attempt_id,
            telegram_user_id=telegram_user_id,
        )
        if creation.availability is not PaymentInitiationAvailability.READY:
            raise ConflictError(
                "PAYMENT_METHOD_UNAVAILABLE",
                creation.unavailable_reason or "This payment method is not available",
            )
        before_order = OrderService._order_state(order)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="payment_attempt",
            resource_id=attempt_id,
        )
        # SQLAlchemy assigns UUID defaults when the idempotency record is
        # inserted.  Flush before using its FK on the attempt so every
        # checkout path works with both SQLite tests and PostgreSQL.
        await self.session.flush()
        method_data = dict(creation.instructions)
        if destination is not None:
            method_data["manual_destination"] = self.manual_destination_snapshot(destination)
        if method == PaymentMethod.TELEGRAM_STARS:
            method_data["telegram_bot_username"] = get_settings().telegram_stars_bot_username
            method_data["telegram_price_version"] = get_settings().telegram_stars_price_version
        attempt = PaymentAttempt(
            id=attempt_id,
            order_id=order.id,
            buyer_user_id=buyer_user_id,
            method=method,
            provider_namespace=creation.provider_namespace,
            status=PaymentAttemptStatus.AWAITING_PAYMENT,
            order_amount_paise=order.total_paise,
            order_currency=order.currency,
            provider_amount=creation.provider_amount,
            provider_currency=creation.provider_currency,
            method_data_snapshot=canonical_payload(method_data),
            merchant_reference=creation.merchant_reference,
            telegram_invoice_payload=self._optional_text(method_data.get("invoice_payload"), maximum=255),
            telegram_user_id=telegram_user_id if method == PaymentMethod.TELEGRAM_STARS else None,
            manual_destination_id=destination.id if destination is not None else None,
            idempotency_record_id=record.id,
            expires_at=order.expires_at,
        )
        self.session.add(attempt)
        require_transition(
            current=order.status,
            target=OrderStatus.AWAITING_PAYMENT,
            transitions=ORDER_TRANSITIONS,
            resource="Order",
        )
        order.status = OrderStatus.AWAITING_PAYMENT
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="payment_attempt",
            entity_id=attempt.id,
            action="PAYMENT_ATTEMPT_CREATED",
            before_state=None,
            after_state=self.attempt_snapshot(attempt),
            reason=f"{method.value} payment instructions were exposed",
        )
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="order",
            entity_id=order.id,
            action="ORDER_PAYMENT_INSTRUCTIONS_EXPOSED",
            before_state=before_order,
            after_state=OrderService._order_state(order),
            reason=method.value,
        )
        await self.session.flush()
        response = self.attempt_mutation_snapshot(order=order, attempt=attempt)
        record.response_payload = response
        await self._finish(commit=commit)
        return PaymentAttemptMutationResult(attempt, response, False)

    async def get_payment_attempt_for_buyer(
        self, *, attempt_id: UUID, buyer_user_id: UUID
    ) -> PaymentAttempt:
        attempt = await get_payment_attempt_for_buyer(
            self.session, attempt_id=attempt_id, buyer_user_id=buyer_user_id
        )
        if attempt is None:
            raise ValidationError("UNKNOWN_PAYMENT_ATTEMPT", "Payment attempt does not exist")
        return attempt

    async def available_methods_for_buyer(self) -> dict[str, bool]:
        """Provide non-authoritative checkout hints without exposing destinations."""
        settings = get_settings()
        manual_upi = settings.manual_upi_enabled and await get_active_manual_destination(self.session) is not None
        return {
            "p2p_match": True,  # A request may still queue or be rejected by order-specific rules.
            "manual_upi": manual_upi,
            "telegram_stars": settings.telegram_stars_enabled,
            "white_label": False,  # Provider adapter does not initiate checkout yet.
        }

    async def create_manual_destination(
        self,
        *,
        actor_user_id: UUID,
        display_label: str,
        upi_id: str,
        qr_reference: str | None,
        instructions: Mapping[str, str],
        approval_evidence_reference: str,
        idempotency_key: str,
        commit: bool,
    ) -> dict[str, object]:
        """Replace the active manual-payment instruction source under admin audit."""

        await self._require_admin(actor_user_id)
        scope = f"admin:{actor_user_id}:manual-upi.destination.create"
        request_fingerprint = fingerprint(
            {
                "display_label": display_label,
                "upi_id": upi_id,
                "qr_reference": qr_reference,
                "instructions": dict(instructions),
                "approval_evidence_reference": approval_evidence_reference,
            }
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            if not isinstance(replay.response_payload, dict):
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior manual destination response is incomplete")
            return dict(replay.response_payload)
        # Row locks do not protect the no-active-destination case.  Pair a
        # transaction lock with the partial unique index so concurrent admin
        # replacements cannot expose two active UPI instruction sets.
        await lock_key_for_transaction(self.session, "manual-upi-active-destination")
        active_rows = list(
            await self.session.scalars(
                select(ManualPaymentDestination)
                .where(ManualPaymentDestination.status == ManualPaymentDestinationStatus.ACTIVE)
                .with_for_update()
            )
        )
        now = datetime.now(timezone.utc)
        for existing in active_rows:
            before_existing = self.manual_destination_snapshot(existing)
            existing.status = ManualPaymentDestinationStatus.DISABLED
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="manual_payment_destination",
                entity_id=existing.id,
                action="MANUAL_PAYMENT_DESTINATION_DISABLED",
                before_state=before_existing,
                after_state=self.manual_destination_snapshot(existing),
                reason="Replaced by a new approved manual payment destination",
            )
        destination = ManualPaymentDestination(
            id=uuid4(),
            display_label=self._required_text(display_label, field="display_label", maximum=120),
            upi_id=self._required_text(upi_id, field="upi_id", maximum=255),
            qr_reference=self._optional_text(qr_reference, maximum=255),
            instructions=json.dumps(canonical_payload(dict(instructions)), sort_keys=True, separators=(",", ":")),
            status=ManualPaymentDestinationStatus.ACTIVE,
            approved_by_user_id=actor_user_id,
            approval_evidence_reference=self._required_text(
                approval_evidence_reference, field="approval_evidence_reference", maximum=255
            ),
            approved_at=now,
        )
        self.session.add(destination)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="manual_payment_destination",
            resource_id=destination.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="manual_payment_destination",
            entity_id=destination.id,
            action="MANUAL_PAYMENT_DESTINATION_APPROVED",
            before_state=None,
            after_state=self.manual_destination_snapshot(destination),
            reason=destination.approval_evidence_reference,
        )
        await self.session.flush()
        response = self.manual_destination_snapshot(destination)
        record.response_payload = response
        await self._finish(commit=commit)
        return response

    async def submit_manual_proof(
        self,
        *,
        attempt_id: UUID,
        buyer_user_id: UUID,
        utr: str,
        proof_reference: str,
        proof_metadata: Mapping[str, str] | None,
        submitted_amount_paise: int,
        submitted_currency: str,
        idempotency_key: str,
        commit: bool,
    ) -> ManualProofMutationResult:
        """Persist manual evidence and route it to review; never settle it here."""

        context = await self._locked_attempt_context(attempt_id)
        if context.attempt.buyer_user_id != buyer_user_id:
            raise AuthorizationError("You cannot submit proof for this payment attempt")
        if context.attempt.method != PaymentMethod.MANUAL_UPI or context.attempt.manual_destination_id is None:
            raise ConflictError("INVALID_PAYMENT_METHOD", "Payment attempt does not accept manual UPI proof")
        utr = self._required_text(utr, field="utr", maximum=160)
        proof_reference = self._required_text(proof_reference, field="proof_reference", maximum=255)
        if isinstance(submitted_amount_paise, bool) or not isinstance(submitted_amount_paise, int) or submitted_amount_paise <= 0:
            raise ValidationError("INVALID_AMOUNT", "submitted_amount_paise must be a positive integer")
        if not isinstance(submitted_currency, str) or submitted_currency.upper() != "INR":
            raise ValidationError("INVALID_CURRENCY", "submitted_currency must be INR")
        scope = f"user:{buyer_user_id}:manual-upi.proof"
        request_fingerprint = fingerprint(
            {
                "attempt_id": attempt_id,
                "utr": utr,
                "proof_reference": proof_reference,
                "proof_metadata": dict(proof_metadata) if proof_metadata is not None else None,
                "submitted_amount_paise": submitted_amount_paise,
                "submitted_currency": "INR",
            }
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_manual_proof(replay)
        if context.attempt.status not in {
            PaymentAttemptStatus.AWAITING_PAYMENT,
            PaymentAttemptStatus.EXPIRED,
        }:
            raise ConflictError("PAYMENT_NOT_AWAITING_PROOF", "Payment attempt cannot accept another proof")
        now = datetime.now(timezone.utc)
        # A manual proof is evidence, not a provider settlement.  Accept it
        # after expiry only into explicit human review, whether the expiry
        # worker ran first or not.  That makes the outcome independent of the
        # scheduler race and preserves the evidence for reconciliation.
        late_evidence = (
            context.attempt.status == PaymentAttemptStatus.EXPIRED
            or self._as_utc(context.attempt.expires_at) <= now
        )
        await lock_key_for_transaction(self.session, "manual-upi-utr", utr)
        existing = await get_manual_proof_by_utr(self.session, utr=utr, for_update=True)
        if existing is not None:
            if existing.payment_attempt_id == context.attempt.id:
                raise ConflictError("PROOF_ALREADY_SUBMITTED", "A proof with this UTR already exists for the attempt")
            self._route_attempt_to_review(
                context,
                actor_user_id=buyer_user_id,
                action="MANUAL_PAYMENT_PROOF_DUPLICATE_UTR",
                reason="A UTR was already submitted against a different payment attempt",
                failure_code="DUPLICATE_UTR",
            )
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="payment_attempt",
                resource_id=context.attempt.id,
            )
            await self.session.flush()
            response = self.manual_proof_mutation_snapshot(context.order, context.attempt, None, review_required=True)
            record.response_payload = response
            await self._finish(commit=commit)
            return ManualProofMutationResult(context.attempt, None, response, False)

        destination = await get_manual_destination(
            self.session, context.attempt.manual_destination_id, for_update=True
        )
        if destination is None:
            raise ConflictError("MANUAL_DESTINATION_MISSING", "The snapshotted manual destination is unavailable")
        ManualUPIAdapter(
            configuration=ManualUPIConfiguration(
                upi_id=destination.upi_id,
                payee_name=destination.display_label,
                qr_image_reference=destination.qr_reference,
            )
        ).build_proof_evidence(
            merchant_reference=context.attempt.merchant_reference,
            utr=utr,
            proof_reference=proof_reference,
            submitted_by=str(buyer_user_id),
        )
        proof = ManualPaymentProof(
            id=uuid4(),
            payment_attempt_id=context.attempt.id,
            manual_destination_id=destination.id,
            buyer_user_id=buyer_user_id,
            utr=utr,
            proof_reference=proof_reference,
            proof_metadata=canonical_payload(dict(proof_metadata)) if proof_metadata is not None else None,
            submitted_amount_paise=submitted_amount_paise,
            submitted_currency="INR",
            submitted_at=now,
            status=ManualPaymentProofStatus.SUBMITTED,
        )
        self.session.add(proof)
        before_attempt = self.attempt_snapshot(context.attempt)
        before_order = OrderService._order_state(context.order)
        context.attempt.status = PaymentAttemptStatus.UNDER_REVIEW
        if late_evidence:
            context.attempt.expired_at = context.attempt.expired_at or now
            context.attempt.failure_code = "LATE_MANUAL_PROOF"
            context.attempt.failure_reason = (
                "Manual proof arrived after payment instructions expired and requires explicit review"
            )
        self._move_order_to_payment_review(context.order)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="manual_payment_proof",
            resource_id=proof.id,
        )
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="manual_payment_proof",
            entity_id=proof.id,
            action=(
                "MANUAL_PAYMENT_PROOF_LATE_SUBMITTED"
                if late_evidence
                else "MANUAL_PAYMENT_PROOF_SUBMITTED"
            ),
            before_state=None,
            after_state=self.manual_proof_snapshot(proof),
            reason=(
                "Late proof is retained for explicit review and is not payment verification"
                if late_evidence
                else "Proof is queued for manual review and is not payment verification"
            ),
        )
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="payment_attempt",
            entity_id=context.attempt.id,
            action=(
                "MANUAL_PAYMENT_ATTEMPT_LATE_PROOF_REVIEW_REQUIRED"
                if late_evidence
                else "MANUAL_PAYMENT_ATTEMPT_REVIEW_REQUIRED"
            ),
            before_state=before_attempt,
            after_state=self.attempt_snapshot(context.attempt),
            reason=(
                "Late manual proof is evidence only and requires explicit approval"
                if late_evidence
                else "A screenshot/UTR is evidence only"
            ),
        )
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="order",
            entity_id=context.order.id,
            action="ORDER_PAYMENT_REVIEW_REQUIRED",
            before_state=before_order,
            after_state=OrderService._order_state(context.order),
            reason="Late manual UPI proof submitted" if late_evidence else "Manual UPI proof submitted",
        )
        await self.session.flush()
        response = self.manual_proof_mutation_snapshot(context.order, context.attempt, proof, review_required=True)
        record.response_payload = response
        await self._finish(commit=commit)
        return ManualProofMutationResult(context.attempt, proof, response, False)

    async def review_manual_proof(
        self,
        *,
        attempt_id: UUID,
        actor_user_id: UUID,
        decision: str,
        review_note: str,
        idempotency_key: str,
        commit: bool,
    ) -> ManualProofMutationResult:
        """Apply an explicit, audited administrator decision to one proof."""

        await self._require_admin(actor_user_id)
        context = await self._locked_attempt_context(attempt_id)
        proof = await get_manual_proof_for_attempt(self.session, attempt_id=attempt_id, for_update=True)
        if proof is None:
            raise ValidationError("UNKNOWN_MANUAL_PAYMENT_PROOF", "Manual payment proof does not exist")
        decision = self._required_text(decision, field="decision", maximum=40).upper()
        if decision not in {"APPROVE", "REJECT", "KEEP_IN_REVIEW"}:
            raise ValidationError("INVALID_MANUAL_REVIEW_DECISION", "Unsupported manual payment review decision")
        review_note = self._required_text(review_note, field="review_note", maximum=500)
        scope = f"admin:{actor_user_id}:manual-upi.review"
        request_fingerprint = fingerprint(
            {"attempt_id": attempt_id, "proof_id": proof.id, "decision": decision, "review_note": review_note}
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_manual_proof(replay)
        if proof.status not in {ManualPaymentProofStatus.SUBMITTED, ManualPaymentProofStatus.UNDER_REVIEW}:
            raise ConflictError("MANUAL_PROOF_ALREADY_DECIDED", "Manual payment proof is already terminal")

        if decision == "APPROVE" and (
            proof.submitted_amount_paise != context.attempt.order_amount_paise
            or proof.submitted_currency != context.attempt.order_currency
        ):
            before_proof = self.manual_proof_snapshot(proof)
            proof.status = ManualPaymentProofStatus.UNDER_REVIEW
            proof.reviewed_by_user_id = actor_user_id
            proof.reviewed_at = datetime.now(timezone.utc)
            proof.review_note = review_note
            self._route_attempt_to_review(
                context,
                actor_user_id=actor_user_id,
                action="MANUAL_PAYMENT_APPROVAL_AMOUNT_MISMATCH",
                reason="Manual proof amount/currency does not equal the frozen payment attempt",
                failure_code="AMOUNT_MISMATCH",
            )
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="manual_payment_proof",
                resource_id=proof.id,
            )
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="manual_payment_proof",
                entity_id=proof.id,
                action="MANUAL_PAYMENT_PROOF_AMOUNT_MISMATCH",
                before_state=before_proof,
                after_state=self.manual_proof_snapshot(proof),
                reason=review_note,
            )
            await self.session.flush()
            response = self.manual_proof_mutation_snapshot(context.order, context.attempt, proof, review_required=True)
            record.response_payload = response
            await self._finish(commit=commit)
            return ManualProofMutationResult(context.attempt, proof, response, False)

        if decision == "APPROVE":
            return await self._settle_manual_proof(
                context=context,
                proof=proof,
                actor_user_id=actor_user_id,
                scope=scope,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                review_note=review_note,
                commit=commit,
            )

        before_proof = self.manual_proof_snapshot(proof)
        before_attempt = self.attempt_snapshot(context.attempt)
        before_order = OrderService._order_state(context.order)
        proof.reviewed_by_user_id = actor_user_id
        proof.reviewed_at = datetime.now(timezone.utc)
        proof.review_note = review_note
        if decision == "REJECT":
            proof.status = ManualPaymentProofStatus.REJECTED
            context.attempt.status = PaymentAttemptStatus.FAILED
            context.attempt.failed_at = proof.reviewed_at
            context.attempt.failure_code = "MANUAL_PROOF_REJECTED"
            context.attempt.failure_reason = review_note
            attempt_action = "MANUAL_PAYMENT_ATTEMPT_REJECTED"
            proof_action = "MANUAL_PAYMENT_PROOF_REJECTED"
        else:
            proof.status = ManualPaymentProofStatus.UNDER_REVIEW
            context.attempt.status = PaymentAttemptStatus.UNDER_REVIEW
            context.attempt.failure_code = None
            context.attempt.failure_reason = None
            attempt_action = "MANUAL_PAYMENT_ATTEMPT_RETAINED_FOR_REVIEW"
            proof_action = "MANUAL_PAYMENT_PROOF_RETAINED_FOR_REVIEW"
        self._move_order_to_payment_review(context.order)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="manual_payment_proof",
            resource_id=proof.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="manual_payment_proof",
            entity_id=proof.id,
            action=proof_action,
            before_state=before_proof,
            after_state=self.manual_proof_snapshot(proof),
            reason=review_note,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="payment_attempt",
            entity_id=context.attempt.id,
            action=attempt_action,
            before_state=before_attempt,
            after_state=self.attempt_snapshot(context.attempt),
            reason=review_note,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="order",
            entity_id=context.order.id,
            action="ORDER_PAYMENT_REVIEW_DECISION_RECORDED",
            before_state=before_order,
            after_state=OrderService._order_state(context.order),
            reason=review_note,
        )
        await self.session.flush()
        response = self.manual_proof_mutation_snapshot(context.order, context.attempt, proof, review_required=True)
        record.response_payload = response
        await self._finish(commit=commit)
        return ManualProofMutationResult(context.attempt, proof, response, False)

    async def list_manual_proofs_for_review(self, *, actor_user_id: UUID, limit: int) -> list[ManualPaymentProof]:
        await self._require_admin(actor_user_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1_000:
            raise ValidationError("INVALID_PAGE_SIZE", "limit must be an integer between 1 and 1000")
        return await list_manual_proofs_for_review(self.session, limit=limit)

    async def ingest_white_label_event(
        self,
        *,
        payload: WhiteLabelWebhookRequest,
        raw_body: bytes,
        signature: str | None,
        request_headers: Mapping[str, str] | None,
        commit: bool,
    ) -> PaymentProviderEventMutationResult:
        """Authenticate, retain, and safely process a provider-neutral callback."""

        settings = get_settings()
        if not settings.white_label_enabled or not settings.white_label_provider_namespace:
            raise AuthorizationError("White-label payments are disabled until provider configuration is complete")
        if payload.provider_namespace != settings.white_label_provider_namespace:
            raise ValidationError("UNEXPECTED_PROVIDER_NAMESPACE", "Webhook provider namespace is not configured")
        try:
            WhiteLabelAdapter(
                signing_secret=settings.white_label_webhook_secret,
                provider_namespace=settings.white_label_provider_namespace,
            ).verify_webhook_signature(raw_body=raw_body, signature=signature)
        except PaymentAdapterConfigurationError as error:
            raise AuthorizationError(str(error)) from error
        except PaymentEvidenceValidationError as error:
            raise AuthenticationError(str(error)) from error
        return await self._ingest_white_label_authenticated(
            payload=payload, raw_body=raw_body, request_headers=request_headers, commit=commit
        )

    async def ingest_telegram_update(
        self,
        *,
        payload: TelegramUpdate,
        raw_body: bytes,
        webhook_secret: str | None,
        commit: bool,
    ) -> PaymentProviderEventMutationResult:
        """Authenticate a Telegram update and process only safe update types."""

        if not get_settings().telegram_stars_enabled:
            raise AuthorizationError(
                "Telegram Stars is disabled until bot, webhook, payload-signing, and Bot API acknowledgement settings are complete"
            )
        adapter = self.telegram_adapter()
        try:
            adapter.verify_webhook_secret(webhook_secret)
        except PaymentAdapterConfigurationError as error:
            raise AuthorizationError(str(error)) from error
        except PaymentEvidenceValidationError as error:
            raise AuthenticationError(str(error)) from error
        return await self._ingest_telegram_authenticated(payload=payload, raw_body=raw_body, commit=commit)

    async def expire_payment_attempt(
        self, *, attempt_id: UUID, idempotency_key: str, commit: bool
    ) -> bool:
        """Move an expired exposed payment to reconciliation/review, never release it."""

        context = await self._locked_attempt_context(attempt_id)
        scope = "system:payment-attempt-expiry"
        request_fingerprint = fingerprint({"attempt_id": attempt_id, "action": "expire"})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            # The original transition has already been durably recorded.  A
            # worker replay must not claim another expiry or create another
            # audit trail.
            return False
        if context.attempt.status != PaymentAttemptStatus.AWAITING_PAYMENT:
            return False
        now = datetime.now(timezone.utc)
        if self._as_utc(context.attempt.expires_at) > now:
            return False
        before_attempt = self.attempt_snapshot(context.attempt)
        before_order = OrderService._order_state(context.order)
        context.attempt.status = PaymentAttemptStatus.EXPIRED
        context.attempt.expired_at = now
        context.attempt.failure_code = "PAYMENT_INSTRUCTIONS_EXPIRED"
        context.attempt.failure_reason = "External payment instructions expired and require reconciliation"
        self._move_order_to_payment_review(context.order)
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="payment_attempt",
            resource_id=context.attempt.id,
        )
        self.audit.record(
            actor_user_id=None,
            entity_type="payment_attempt",
            entity_id=context.attempt.id,
            action="PAYMENT_ATTEMPT_EXPIRED_RECONCILIATION_REQUIRED",
            before_state=before_attempt,
            after_state=self.attempt_snapshot(context.attempt),
            reason=context.attempt.failure_reason,
        )
        self.audit.record(
            actor_user_id=None,
            entity_type="order",
            entity_id=context.order.id,
            action="ORDER_PAYMENT_REVIEW_REQUIRED",
            before_state=before_order,
            after_state=OrderService._order_state(context.order),
            reason="Payment instruction expiry never auto-releases an exposed order",
        )
        await self.session.flush()
        record.response_payload = canonical_payload(
            {
                "payment": self.attempt_snapshot(context.attempt),
                "order": OrderService.snapshot(context.order),
            }
        )
        await self._finish(commit=commit)
        return True

    def telegram_adapter(self) -> TelegramStarsAdapter:
        settings = get_settings()
        return TelegramStarsAdapter(
            configuration=TelegramStarsConfiguration(
                bot_token=settings.telegram_stars_bot_token,
                webhook_secret=settings.telegram_stars_webhook_secret,
                invoice_payload_secret=settings.telegram_stars_invoice_payload_secret,
                allow_bot_api_calls=settings.telegram_stars_allow_bot_api_calls,
            )
        )

    async def _build_creation(
        self,
        *,
        method: PaymentMethod,
        order: Order,
        buyer_user_id: UUID,
        attempt_id: UUID,
        telegram_user_id: int | None,
    ) -> tuple[PaymentCreation, ManualPaymentDestination | None]:
        merchant_reference = f"pay-{attempt_id.hex}"
        if method == PaymentMethod.MANUAL_UPI:
            if not get_settings().manual_upi_enabled:
                raise ConflictError("PAYMENT_METHOD_UNAVAILABLE", "Manual UPI is disabled")
            destination = await get_active_manual_destination(self.session, for_update=True)
            if destination is None:
                raise ConflictError("PAYMENT_METHOD_UNAVAILABLE", "No approved manual UPI destination is active")
            creation = ManualUPIAdapter(
                configuration=ManualUPIConfiguration(
                    upi_id=destination.upi_id,
                    payee_name=destination.display_label,
                    qr_image_reference=destination.qr_reference,
                )
            ).create_payment(
                PaymentCreationRequest(
                    order_id=order.id,
                    customer_reference=str(buyer_user_id),
                    amount_paise=order.total_paise,
                    currency=order.currency,
                    provider_amount=order.total_paise,
                    provider_currency="INR",
                    merchant_reference=merchant_reference,
                )
            )
            return creation, destination

        if method == PaymentMethod.TELEGRAM_STARS:
            settings = get_settings()
            if not settings.telegram_stars_enabled:
                raise ConflictError(
                    "PAYMENT_METHOD_UNAVAILABLE",
                    "Telegram Stars is disabled until bot, webhook, and payload-signing configuration is complete",
                )
            if telegram_user_id is None or isinstance(telegram_user_id, bool) or telegram_user_id <= 0:
                raise ValidationError("TELEGRAM_USER_REQUIRED", "telegram_user_id is required for Telegram Stars")
            stars_amount = (order.total_paise * settings.telegram_stars_per_inr + 99) // 100
            creation = self.telegram_adapter().create_payment(
                PaymentCreationRequest(
                    order_id=order.id,
                    customer_reference=str(telegram_user_id),
                    amount_paise=order.total_paise,
                    currency=order.currency,
                    provider_amount=stars_amount,
                    provider_currency=TELEGRAM_STARS_CURRENCY,
                    merchant_reference=merchant_reference,
                )
            )
            return creation, None

        if method == PaymentMethod.WHITE_LABEL:
            settings = get_settings()
            creation = WhiteLabelAdapter(
                signing_secret=settings.white_label_webhook_secret,
                provider_namespace=settings.white_label_provider_namespace or "white_label",
            ).create_payment(
                PaymentCreationRequest(
                    order_id=order.id,
                    customer_reference=str(buyer_user_id),
                    amount_paise=order.total_paise,
                    currency=order.currency,
                    provider_amount=order.total_paise,
                    provider_currency="INR",
                    merchant_reference=merchant_reference,
                )
            )
            return creation, None
        raise ValidationError("INVALID_PAYMENT_METHOD", "Unsupported payment method")

    async def _ingest_white_label_authenticated(
        self,
        *,
        payload: WhiteLabelWebhookRequest,
        raw_body: bytes,
        request_headers: Mapping[str, str] | None,
        commit: bool,
    ) -> PaymentProviderEventMutationResult:
        digest = sha256(raw_body).hexdigest()
        await lock_key_for_transaction(
            self.session, "payment-provider-event", payload.provider_namespace, payload.external_event_id
        )
        existing = await get_payment_provider_event(
            self.session,
            provider_namespace=payload.provider_namespace,
            external_event_id=payload.external_event_id,
            for_update=True,
        )
        if existing is not None:
            if existing.payload_digest != digest:
                before = self.provider_event_snapshot(existing)
                existing.status = PaymentProviderEventStatus.REVIEW_REQUIRED
                existing.processing_error = "REPLAY_PAYLOAD_DIGEST_CONFLICT"
                existing.processed_at = datetime.now(timezone.utc)
                self.audit.record(
                    actor_user_id=None,
                    entity_type="payment_provider_event",
                    entity_id=existing.id,
                    action="PAYMENT_PROVIDER_EVENT_REPLAY_CONFLICT",
                    before_state=before,
                    after_state=self.provider_event_snapshot(existing),
                    reason="The same external event ID arrived with a different signed payload digest",
                )
                await self._finish(commit=commit)
                return PaymentProviderEventMutationResult(existing, self.provider_event_response(existing), False)
            return PaymentProviderEventMutationResult(existing, self.provider_event_response(existing), True)

        observed_attempt = await get_payment_attempt_by_merchant_reference(
            self.session, merchant_reference=payload.merchant_reference
        )
        context = await self._locked_attempt_context(observed_attempt.id) if observed_attempt else None
        now = datetime.now(timezone.utc)
        event = PaymentProviderEvent(
            id=uuid4(),
            provider_namespace=payload.provider_namespace,
            external_event_id=payload.external_event_id,
            event_type=payload.event_type,
            payload_digest=digest,
            raw_payload=canonical_payload(payload.model_dump(mode="json")),
            request_headers=self._safe_headers(request_headers),
            payment_attempt_id=context.attempt.id if context else None,
            merchant_reference=payload.merchant_reference,
            provider_payment_reference=payload.provider_payment_reference,
            provider_amount=payload.provider_amount,
            provider_currency=payload.provider_currency,
            occurred_at=self._as_utc(payload.occurred_at) if payload.occurred_at else None,
            signature_verified_at=now,
            status=PaymentProviderEventStatus.PROCESSING,
        )
        self.session.add(event)
        await self.session.flush()
        before = self.provider_event_snapshot(event)
        settlement: PaymentSettlement | None = None
        review_reason: str | None = None
        if context is None:
            review_reason = "UNKNOWN_MERCHANT_REFERENCE"
        elif context.attempt.method != PaymentMethod.WHITE_LABEL:
            review_reason = "PAYMENT_METHOD_MISMATCH"
        elif context.attempt.provider_namespace != payload.provider_namespace:
            review_reason = "PROVIDER_NAMESPACE_MISMATCH"
        elif context.attempt.merchant_reference != payload.merchant_reference:
            review_reason = "MERCHANT_REFERENCE_MISMATCH"
        elif payload.event_type == "PAYMENT_SUCCEEDED" and not payload.provider_payment_reference:
            # A success without a provider-side immutable payment reference
            # cannot be deduplicated safely, so retain it for review rather
            # than creating a financial settlement.
            review_reason = "MISSING_PROVIDER_PAYMENT_REFERENCE"
        elif payload.event_type == "PAYMENT_SUCCEEDED" and (
            context.attempt.provider_amount != payload.provider_amount
            or context.attempt.provider_currency != payload.provider_currency
        ):
            review_reason = "AMOUNT_OR_CURRENCY_MISMATCH"
        elif payload.event_type == "PAYMENT_SUCCEEDED" and self._as_utc(context.attempt.expires_at) <= now:
            review_reason = "LATE_PROVIDER_EVENT"

        if review_reason is None and payload.provider_payment_reference:
            await lock_key_for_transaction(
                self.session,
                "payment-provider-reference",
                payload.provider_namespace,
                payload.provider_payment_reference,
            )
            existing_reference = await get_payment_attempt_by_provider_reference(
                self.session,
                provider_namespace=payload.provider_namespace,
                provider_payment_reference=payload.provider_payment_reference,
                for_update=True,
            )
            if existing_reference is not None and context is not None and existing_reference.id != context.attempt.id:
                review_reason = "PROVIDER_PAYMENT_REFERENCE_REUSED"

        if review_reason is not None:
            event.status = PaymentProviderEventStatus.REVIEW_REQUIRED
            event.processing_error = review_reason
            if context is not None:
                self._route_attempt_to_review(
                    context,
                    actor_user_id=None,
                    action="PROVIDER_EVENT_REVIEW_REQUIRED",
                    reason=review_reason,
                    failure_code=review_reason,
                )
        elif payload.event_type == "PAYMENT_SUCCEEDED":
            assert context is not None and payload.provider_payment_reference is not None
            context.attempt.provider_payment_reference = payload.provider_payment_reference
            settlement = await self._settle_attempt(
                context=context,
                actor_user_id=None,
                scope=f"provider:{payload.provider_namespace}:payment.settlement",
                # Provider IDs may be shorter than the platform's mutation
                # key minimum. Prefix them into a bounded, stable key rather
                # than rejecting an otherwise valid signed callback.
                idempotency_key=(
                    f"provider-event:{payload.provider_namespace}:{payload.external_event_id}"
                ),
                request_fingerprint=fingerprint(
                    {
                        "payment_attempt_id": context.attempt.id,
                        "external_event_id": payload.external_event_id,
                        "provider_payment_reference": payload.provider_payment_reference,
                        "amount": payload.provider_amount,
                        "currency": payload.provider_currency,
                    }
                ),
                verification_source=f"WHITE_LABEL_WEBHOOK:{payload.provider_namespace}",
                provider_event=event,
                manual_proof=None,
                commit=False,
            )
            event.status = PaymentProviderEventStatus.PROCESSED
            event.processing_error = None
        elif payload.event_type == "PAYMENT_PENDING":
            event.status = PaymentProviderEventStatus.PROCESSED
            event.processing_error = None
        else:
            assert context is not None
            self._route_attempt_to_review(
                context,
                actor_user_id=None,
                action="PROVIDER_PAYMENT_FAILURE_REVIEW_REQUIRED",
                reason="Provider reported a failed payment; reconciliation is required",
                failure_code="PROVIDER_PAYMENT_FAILED",
            )
            event.status = PaymentProviderEventStatus.PROCESSED
            event.processing_error = "PROVIDER_PAYMENT_FAILED_RECONCILIATION_REQUIRED"

        event.processed_at = now
        self.audit.record(
            actor_user_id=None,
            entity_type="payment_provider_event",
            entity_id=event.id,
            action="PAYMENT_PROVIDER_EVENT_PROCESSED",
            before_state=before,
            after_state=self.provider_event_snapshot(event),
            reason=event.processing_error,
        )
        await self._finish(commit=commit)
        return PaymentProviderEventMutationResult(
            event, self.provider_event_response(event, settlement_id=settlement.id if settlement else None), False
        )

    async def _ingest_telegram_authenticated(
        self, *, payload: TelegramUpdate, raw_body: bytes, commit: bool
    ) -> PaymentProviderEventMutationResult:
        digest = sha256(raw_body).hexdigest()
        external_event_id = str(payload.update_id)
        await lock_key_for_transaction(self.session, "telegram-update", external_event_id)
        existing = await get_payment_provider_event(
            self.session,
            provider_namespace=TELEGRAM_STARS_NAMESPACE,
            external_event_id=external_event_id,
            for_update=True,
        )
        if existing is not None:
            if existing.payload_digest != digest:
                before = self.provider_event_snapshot(existing)
                existing.status = PaymentProviderEventStatus.REVIEW_REQUIRED
                existing.processing_error = "REPLAY_PAYLOAD_DIGEST_CONFLICT"
                existing.processed_at = datetime.now(timezone.utc)
                self.audit.record(
                    actor_user_id=None,
                    entity_type="payment_provider_event",
                    entity_id=existing.id,
                    action="TELEGRAM_UPDATE_REPLAY_CONFLICT",
                    before_state=before,
                    after_state=self.provider_event_snapshot(existing),
                    reason="Telegram update ID replayed with a different body digest",
                )
                await self._finish(commit=commit)
                return PaymentProviderEventMutationResult(existing, self.provider_event_response(existing), False)
            return PaymentProviderEventMutationResult(existing, self.provider_event_response(existing), True)

        if payload.pre_checkout_query is not None:
            return await self._ingest_telegram_pre_checkout(payload=payload, digest=digest, commit=commit)
        successful = payload.message.successful_payment if payload.message is not None else None
        if successful is not None and payload.message is not None:
            return await self._ingest_telegram_successful_payment(payload=payload, digest=digest, commit=commit)
        return await self._record_unhandled_telegram_update(payload=payload, digest=digest, commit=commit)

    async def _ingest_telegram_pre_checkout(
        self, *, payload: TelegramUpdate, digest: str, commit: bool
    ) -> PaymentProviderEventMutationResult:
        query = payload.pre_checkout_query
        assert query is not None
        observed_attempt = await get_payment_attempt_by_telegram_payload(
            self.session, invoice_payload=query.invoice_payload
        )
        context = await self._locked_attempt_context(observed_attempt.id) if observed_attempt else None
        now = datetime.now(timezone.utc)
        event = PaymentProviderEvent(
            id=uuid4(),
            provider_namespace=TELEGRAM_STARS_NAMESPACE,
            external_event_id=str(payload.update_id),
            event_type="TELEGRAM_PRE_CHECKOUT",
            payload_digest=digest,
            raw_payload=canonical_payload(payload.model_dump(mode="json")),
            request_headers={"authentication": "telegram-webhook-secret"},
            payment_attempt_id=context.attempt.id if context else None,
            merchant_reference=context.attempt.merchant_reference if context else None,
            provider_amount=query.total_amount,
            provider_currency=query.currency,
            telegram_user_id=query.from_user.id,
            signature_verified_at=now,
            status=PaymentProviderEventStatus.PROCESSING,
        )
        self.session.add(event)
        await self.session.flush()
        before = self.provider_event_snapshot(event)
        approved = False
        error_message: str | None = None
        if context is None:
            error_message = "Payment is unavailable."
            event.processing_error = "UNKNOWN_TELEGRAM_INVOICE_PAYLOAD"
        else:
            try:
                self._validate_telegram_attempt(context)
                if self._as_utc(context.attempt.expires_at) <= now:
                    raise PaymentEvidenceValidationError("Payment instructions have expired")
                self.telegram_adapter().validate_pre_checkout(
                    invoice_payload=query.invoice_payload,
                    total_amount=query.total_amount,
                    currency=query.currency,
                    telegram_user_id=query.from_user.id,
                    expected_order_id=context.order.id,
                    expected_telegram_user_id=context.attempt.telegram_user_id or 0,
                    expected_stars_amount=context.attempt.provider_amount,
                )
                approved = True
            except (PaymentEvidenceValidationError, ValidationError, ConflictError) as error:
                error_message = "Payment details do not match this order."
                event.processing_error = self._error_code(error)
                self._route_attempt_to_review(
                    context,
                    actor_user_id=None,
                    action="TELEGRAM_PRE_CHECKOUT_REVIEW_REQUIRED",
                    reason=event.processing_error,
                    failure_code=event.processing_error,
                )
        event.status = PaymentProviderEventStatus.PROCESSED if approved else PaymentProviderEventStatus.REVIEW_REQUIRED
        event.processed_at = now
        self.audit.record(
            actor_user_id=None,
            entity_type="payment_provider_event",
            entity_id=event.id,
            action="TELEGRAM_PRE_CHECKOUT_VALIDATED",
            before_state=before,
            after_state=self.provider_event_snapshot(event),
            reason=event.processing_error,
        )
        await self._finish(commit=commit)
        return PaymentProviderEventMutationResult(
            event,
            self.provider_event_response(event),
            False,
            pre_checkout_query_id=query.id,
            pre_checkout_approved=approved,
            pre_checkout_error=error_message,
        )

    async def _ingest_telegram_successful_payment(
        self, *, payload: TelegramUpdate, digest: str, commit: bool
    ) -> PaymentProviderEventMutationResult:
        assert payload.message is not None and payload.message.successful_payment is not None
        successful = payload.message.successful_payment
        observed_attempt = await get_payment_attempt_by_telegram_payload(
            self.session, invoice_payload=successful.invoice_payload
        )
        context = await self._locked_attempt_context(observed_attempt.id) if observed_attempt else None
        now = datetime.now(timezone.utc)
        await lock_key_for_transaction(self.session, "telegram-charge", successful.telegram_payment_charge_id)
        existing_charge_attempt = await get_payment_attempt_by_provider_reference(
            self.session,
            provider_namespace=TELEGRAM_STARS_NAMESPACE,
            provider_payment_reference=successful.telegram_payment_charge_id,
            for_update=True,
        )
        charge_conflict = existing_charge_attempt is not None and (
            context is None or existing_charge_attempt.id != context.attempt.id
        )
        same_attempt_charge = existing_charge_attempt is not None and not charge_conflict
        event = PaymentProviderEvent(
            id=uuid4(),
            provider_namespace=TELEGRAM_STARS_NAMESPACE,
            external_event_id=str(payload.update_id),
            event_type="TELEGRAM_SUCCESSFUL_PAYMENT",
            payload_digest=digest,
            raw_payload=canonical_payload(payload.model_dump(mode="json")),
            request_headers={"authentication": "telegram-webhook-secret"},
            payment_attempt_id=context.attempt.id if context else None,
            merchant_reference=context.attempt.merchant_reference if context else None,
            provider_payment_reference=successful.telegram_payment_charge_id,
            provider_amount=successful.total_amount,
            provider_currency=successful.currency,
            telegram_payment_charge_id=None if (charge_conflict or same_attempt_charge) else successful.telegram_payment_charge_id,
            telegram_user_id=payload.message.from_user.id,
            signature_verified_at=now,
            status=PaymentProviderEventStatus.PROCESSING,
        )
        self.session.add(event)
        await self.session.flush()
        before = self.provider_event_snapshot(event)
        settlement: PaymentSettlement | None = None
        review_reason: str | None = None
        if context is None:
            review_reason = "UNKNOWN_TELEGRAM_INVOICE_PAYLOAD"
        elif charge_conflict:
            review_reason = "TELEGRAM_CHARGE_REUSED"
        elif same_attempt_charge:
            settlement = await get_payment_settlement_for_attempt(
                self.session, attempt_id=context.attempt.id, for_update=True
            )
            if settlement is None:
                review_reason = "TELEGRAM_CHARGE_ALREADY_BOUND"
        else:
            try:
                self._validate_telegram_attempt(context)
                if self._as_utc(context.attempt.expires_at) <= now:
                    raise PaymentEvidenceValidationError("Late Telegram successful payment requires reconciliation")
                self.telegram_adapter().validate_successful_payment(
                    merchant_reference=context.attempt.merchant_reference,
                    telegram_payment_charge_id=successful.telegram_payment_charge_id,
                    invoice_payload=successful.invoice_payload,
                    total_amount=successful.total_amount,
                    currency=successful.currency,
                    telegram_user_id=payload.message.from_user.id,
                    expected_order_id=context.order.id,
                    expected_telegram_user_id=context.attempt.telegram_user_id or 0,
                    expected_stars_amount=context.attempt.provider_amount,
                )
            except (PaymentEvidenceValidationError, ValidationError, ConflictError) as error:
                review_reason = self._error_code(error)

        if review_reason is not None:
            event.status = PaymentProviderEventStatus.REVIEW_REQUIRED
            event.processing_error = review_reason
            if context is not None:
                self._route_attempt_to_review(
                    context,
                    actor_user_id=None,
                    action="TELEGRAM_SUCCESSFUL_PAYMENT_REVIEW_REQUIRED",
                    reason=review_reason,
                    failure_code=review_reason,
                )
        elif settlement is None:
            assert context is not None
            context.attempt.provider_payment_reference = successful.telegram_payment_charge_id
            context.attempt.telegram_payment_charge_id = successful.telegram_payment_charge_id
            settlement = await self._settle_attempt(
                context=context,
                actor_user_id=None,
                scope="provider:telegram-stars:payment.settlement",
                # Telegram update IDs are numeric and may be fewer than the
                # eight characters required by the generic idempotency
                # boundary. The namespaced form is stable for replay.
                idempotency_key=f"telegram-update:{payload.update_id}",
                request_fingerprint=fingerprint(
                    {
                        "payment_attempt_id": context.attempt.id,
                        "update_id": payload.update_id,
                        "telegram_payment_charge_id": successful.telegram_payment_charge_id,
                        "stars_amount": successful.total_amount,
                    }
                ),
                verification_source="TELEGRAM_STARS_SUCCESSFUL_PAYMENT",
                provider_event=event,
                manual_proof=None,
                commit=False,
            )
            event.status = PaymentProviderEventStatus.PROCESSED
            event.processing_error = None
        else:
            event.status = PaymentProviderEventStatus.PROCESSED
            event.processing_error = "TELEGRAM_CHARGE_REPLAYED_FOR_SAME_ATTEMPT"
        event.processed_at = now
        self.audit.record(
            actor_user_id=None,
            entity_type="payment_provider_event",
            entity_id=event.id,
            action="TELEGRAM_SUCCESSFUL_PAYMENT_PROCESSED",
            before_state=before,
            after_state=self.provider_event_snapshot(event),
            reason=event.processing_error,
        )
        await self._finish(commit=commit)
        return PaymentProviderEventMutationResult(
            event, self.provider_event_response(event, settlement_id=settlement.id if settlement else None), False
        )

    async def _record_unhandled_telegram_update(
        self, *, payload: TelegramUpdate, digest: str, commit: bool
    ) -> PaymentProviderEventMutationResult:
        now = datetime.now(timezone.utc)
        event = PaymentProviderEvent(
            id=uuid4(),
            provider_namespace=TELEGRAM_STARS_NAMESPACE,
            external_event_id=str(payload.update_id),
            event_type="TELEGRAM_UNHANDLED_UPDATE",
            payload_digest=digest,
            raw_payload=canonical_payload(payload.model_dump(mode="json")),
            request_headers={"authentication": "telegram-webhook-secret"},
            signature_verified_at=now,
            status=PaymentProviderEventStatus.PROCESSED,
            processed_at=now,
        )
        self.session.add(event)
        self.audit.record(
            actor_user_id=None,
            entity_type="payment_provider_event",
            entity_id=event.id,
            action="TELEGRAM_UPDATE_RETAINED_UNHANDLED",
            before_state=None,
            after_state=self.provider_event_snapshot(event),
            reason="No payment state transition is associated with this Telegram update type",
        )
        await self._finish(commit=commit)
        return PaymentProviderEventMutationResult(event, self.provider_event_response(event), False)

    async def _settle_manual_proof(
        self,
        *,
        context: _LockedPaymentContext,
        proof: ManualPaymentProof,
        actor_user_id: UUID,
        scope: str,
        idempotency_key: str,
        request_fingerprint: str,
        review_note: str,
        commit: bool,
    ) -> ManualProofMutationResult:
        before_proof = self.manual_proof_snapshot(proof)
        proof.status = ManualPaymentProofStatus.APPROVED
        proof.reviewed_by_user_id = actor_user_id
        proof.reviewed_at = datetime.now(timezone.utc)
        proof.review_note = review_note
        settlement = await self._settle_attempt(
            context=context,
            actor_user_id=actor_user_id,
            scope=scope,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            verification_source="ADMIN_MANUAL_UPI_REVIEW",
            provider_event=None,
            manual_proof=proof,
            commit=False,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="manual_payment_proof",
            entity_id=proof.id,
            action="MANUAL_PAYMENT_PROOF_APPROVED_AND_SETTLED",
            before_state=before_proof,
            after_state=self.manual_proof_snapshot(proof),
            reason=review_note,
        )
        await self.session.flush()
        response = self.manual_proof_mutation_snapshot(
            context.order, context.attempt, proof, review_required=False, settlement_id=settlement.id
        )
        record = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if record is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Manual settlement did not retain its idempotency record")
        record.response_payload = response
        await self._finish(commit=commit)
        return ManualProofMutationResult(context.attempt, proof, response, False)

    async def _settle_attempt(
        self,
        *,
        context: _LockedPaymentContext,
        actor_user_id: UUID | None,
        scope: str,
        idempotency_key: str,
        request_fingerprint: str,
        verification_source: str,
        provider_event: PaymentProviderEvent | None,
        manual_proof: ManualPaymentProof | None,
        commit: bool,
    ) -> PaymentSettlement:
        """Create one balanced external-receipt settlement and delivery event."""

        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            if replay.resource_id is None:
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior payment settlement lacks a resource")
            settlement = await self.session.get(PaymentSettlement, replay.resource_id)
            if settlement is None:
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior payment settlement cannot be recovered")
            return settlement
        existing = await get_payment_settlement_for_attempt(
            self.session, attempt_id=context.attempt.id, for_update=True
        )
        if existing is not None:
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="payment_settlement",
                resource_id=existing.id,
            )
            record.response_payload = self.settlement_snapshot(existing)
            await self._finish(commit=commit)
            return existing
        if context.order.status not in {OrderStatus.AWAITING_PAYMENT, OrderStatus.PAYMENT_REVIEW}:
            raise ConflictError("ORDER_NOT_AWAITING_PAYMENT", "Order is not eligible for this payment settlement")
        if context.order.settlement_reference_id is not None:
            raise ConflictError("ORDER_ALREADY_SETTLED", "Order already has a settlement reference")
        if context.attempt.status not in {PaymentAttemptStatus.AWAITING_PAYMENT, PaymentAttemptStatus.UNDER_REVIEW}:
            raise ConflictError("PAYMENT_ATTEMPT_NOT_SETTLEABLE", "Payment attempt cannot be settled")
        settlement_id = uuid4()
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="payment_settlement",
            resource_id=settlement_id,
        )
        await self.session.flush()
        clearing = await self.ledger.get_platform_clearing_account(context.order.currency)
        pending = await self.ledger.get_external_order_pending_account(context.order.currency)
        journal = await self.ledger.post_balanced_group(
            event_type="EXTERNAL_ORDER_PAYMENT_SETTLED",
            currency=context.order.currency,
            reference_type="payment_settlement",
            reference_id=settlement_id,
            actor_user_id=actor_user_id,
            reason=verification_source,
            postings=[
                PostingDraft(clearing.id, PostingDirection.DEBIT, context.order.total_paise, context.order.currency),
                PostingDraft(pending.id, PostingDirection.CREDIT, context.order.total_paise, context.order.currency),
            ],
            idempotency_record_id=record.id,
        )
        now = datetime.now(timezone.utc)
        settlement = PaymentSettlement(
            id=settlement_id,
            payment_attempt_id=context.attempt.id,
            order_id=context.order.id,
            journal_group_id=journal.id,
            idempotency_record_id=record.id,
            provider_event_id=provider_event.id if provider_event else None,
            manual_payment_proof_id=manual_proof.id if manual_proof else None,
            amount_paise=context.order.total_paise,
            currency=context.order.currency,
            verification_source=self._required_text(
                verification_source, field="verification_source", maximum=100
            ),
            settled_by_user_id=actor_user_id,
            settled_at=now,
        )
        self.session.add(settlement)
        before_attempt = self.attempt_snapshot(context.attempt)
        before_order = OrderService._order_state(context.order)
        context.attempt.status = PaymentAttemptStatus.SUCCEEDED
        context.attempt.succeeded_at = now
        context.attempt.failed_at = None
        context.attempt.failure_code = None
        context.attempt.failure_reason = None
        require_transition(
            current=context.order.status,
            target=OrderStatus.PAID,
            transitions=ORDER_TRANSITIONS,
            resource="Order",
        )
        context.order.status = OrderStatus.PAID
        context.order.delivery_status = DeliveryStatus.PENDING
        context.order.settlement_reference_id = settlement.id
        context.order.settled_at = now
        await CouponService(self.session).consume_for_settled_order(
            order=context.order, actor_user_id=actor_user_id
        )
        await RevenueService(self.session).allocate_settled_order(
            order=context.order,
            settlement_reference_id=settlement.id,
            source=RevenueAllocationSource.EXTERNAL_ORDER_PENDING,
            actor_user_id=actor_user_id,
            commit=False,
        )
        await ReferralService(self.session).qualify_settled_order(
            order=context.order,
            settlement_reference_id=settlement.id,
            actor_user_id=actor_user_id,
        )
        await CashbackService(self.session).create_pending_reward_for_settled_order(
            order=context.order,
            settlement_reference_id=settlement.id,
            actor_user_id=actor_user_id,
        )
        outbox = PaymentOutboxEvent(
            id=uuid4(),
            payment_attempt_id=context.attempt.id,
            aggregate_type="order",
            aggregate_id=context.order.id,
            event_type=PAYMENT_ORDER_DELIVERY_REQUESTED,
            deduplication_key=f"payment-settlement:{settlement.id}:delivery",
            payload=canonical_payload(
                {
                    "order_id": context.order.id,
                    "settlement_id": settlement.id,
                    "payment_attempt_id": context.attempt.id,
                    "amount_paise": context.order.total_paise,
                }
            ),
            status=PaymentOutboxEventStatus.PENDING,
        )
        self.session.add(outbox)
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="payment_attempt",
            entity_id=context.attempt.id,
            action="PAYMENT_ATTEMPT_SETTLED",
            before_state=before_attempt,
            after_state=self.attempt_snapshot(context.attempt),
            reason=verification_source,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="payment_settlement",
            entity_id=settlement.id,
            action="PAYMENT_SETTLEMENT_RECORDED",
            before_state=None,
            after_state=self.settlement_snapshot(settlement),
            reason=verification_source,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="order",
            entity_id=context.order.id,
            action="ORDER_PAID_DELIVERY_PENDING",
            before_state=before_order,
            after_state=OrderService._order_state(context.order),
            reason=verification_source,
        )
        record.response_payload = self.settlement_snapshot(settlement)
        await self._finish(commit=commit)
        return settlement

    async def _locked_attempt_context(self, attempt_id: UUID) -> _LockedPaymentContext:
        observed = await get_payment_attempt(self.session, attempt_id)
        if observed is None:
            raise ValidationError("UNKNOWN_PAYMENT_ATTEMPT", "Payment attempt does not exist")
        order = await get_order(self.session, observed.order_id, for_update=True)
        if order is None:
            raise ValidationError("UNKNOWN_ORDER", "Payment attempt references an unknown order")
        attempt = await get_payment_attempt(self.session, attempt_id, for_update=True)
        if attempt is None or attempt.order_id != order.id:
            raise ConflictError("PAYMENT_ATTEMPT_CHANGED", "Payment attempt changed during locking")
        return _LockedPaymentContext(order=order, attempt=attempt)

    async def _require_active_user(self, user_id: UUID) -> User:
        user = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == user_id).with_for_update()
        )
        if user is None or not user.is_active:
            raise AuthorizationError("An active user account is required")
        return user

    async def _require_admin(self, user_id: UUID) -> User:
        user = await self._require_active_user(user_id)
        if RoleName.ADMIN.value not in {role.name for role in user.roles}:
            raise AuthorizationError("Administrator role is required")
        return user

    def _route_attempt_to_review(
        self,
        context: _LockedPaymentContext,
        *,
        actor_user_id: UUID | None,
        action: str,
        reason: str,
        failure_code: str,
    ) -> None:
        before_attempt = self.attempt_snapshot(context.attempt)
        before_order = OrderService._order_state(context.order)
        if context.attempt.status not in {PaymentAttemptStatus.SUCCEEDED, PaymentAttemptStatus.CANCELLED}:
            context.attempt.status = PaymentAttemptStatus.UNDER_REVIEW
            context.attempt.failure_code = failure_code[:100]
            context.attempt.failure_reason = reason[:500]
        self._move_order_to_payment_review(context.order)
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="payment_attempt",
            entity_id=context.attempt.id,
            action=action,
            before_state=before_attempt,
            after_state=self.attempt_snapshot(context.attempt),
            reason=reason[:500],
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="order",
            entity_id=context.order.id,
            action="ORDER_PAYMENT_REVIEW_REQUIRED",
            before_state=before_order,
            after_state=OrderService._order_state(context.order),
            reason=reason[:500],
        )

    @staticmethod
    def _move_order_to_payment_review(order: Order) -> None:
        if order.status == OrderStatus.PAYMENT_REVIEW:
            return
        if order.status == OrderStatus.AWAITING_PAYMENT:
            require_transition(
                current=order.status,
                target=OrderStatus.PAYMENT_REVIEW,
                transitions=ORDER_TRANSITIONS,
                resource="Order",
            )
            order.status = OrderStatus.PAYMENT_REVIEW
            return
        if order.status in {OrderStatus.PAID, OrderStatus.FULFILLED}:
            return
        raise ConflictError("ORDER_NOT_REVIEWABLE", "Order cannot be moved into payment review")

    def _validate_telegram_attempt(self, context: _LockedPaymentContext) -> None:
        if context.attempt.method != PaymentMethod.TELEGRAM_STARS:
            raise ValidationError("PAYMENT_METHOD_MISMATCH", "Invoice does not belong to a Telegram Stars attempt")
        if context.attempt.provider_namespace != TELEGRAM_STARS_NAMESPACE:
            raise ValidationError("PROVIDER_NAMESPACE_MISMATCH", "Telegram attempt has an invalid provider namespace")
        if context.attempt.provider_currency != TELEGRAM_STARS_CURRENCY:
            raise ValidationError("PROVIDER_CURRENCY_MISMATCH", "Telegram attempt has an invalid Stars currency")
        if context.attempt.telegram_user_id is None or context.attempt.telegram_invoice_payload is None:
            raise ValidationError("TELEGRAM_ATTEMPT_INCOMPLETE", "Telegram attempt lacks frozen invoice identity")
        if context.attempt.status not in {PaymentAttemptStatus.AWAITING_PAYMENT, PaymentAttemptStatus.UNDER_REVIEW}:
            raise ConflictError("PAYMENT_ATTEMPT_NOT_ACTIVE", "Telegram attempt is not active")

    async def _replay_attempt(self, record: IdempotencyRecord) -> PaymentAttemptMutationResult:
        if record.resource_id is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior payment attempt has no resource")
        attempt = await get_payment_attempt(self.session, record.resource_id)
        if attempt is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior payment attempt cannot be recovered")
        return PaymentAttemptMutationResult(attempt, dict(record.response_payload), True)

    async def _replay_manual_proof(self, record: IdempotencyRecord) -> ManualProofMutationResult:
        if record.resource_id is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior manual payment response is incomplete")
        payload = dict(record.response_payload)
        payment = payload.get("payment")
        if not isinstance(payment, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior manual payment response lacks a payment")
        try:
            attempt_id = UUID(str(payment["id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior manual payment response is invalid") from error
        attempt = await get_payment_attempt(self.session, attempt_id)
        if attempt is None:
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior payment attempt cannot be recovered")
        proof_value = payload.get("proof")
        proof = None
        if isinstance(proof_value, dict):
            try:
                proof_id = UUID(str(proof_value["id"]))
            except (KeyError, TypeError, ValueError) as error:
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior manual proof response is invalid") from error
            proof = await self.session.get(ManualPaymentProof, proof_id)
            if proof is None:
                raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior manual proof cannot be recovered")
        return ManualProofMutationResult(attempt, proof, payload, True)

    @staticmethod
    def _safe_headers(headers: Mapping[str, str] | None) -> dict[str, object] | None:
        if headers is None:
            return None
        allowed = {"content-type", "user-agent", "x-request-id"}
        result: dict[str, object] = {}
        for key, value in headers.items():
            if isinstance(key, str) and isinstance(value, str) and key.lower() in allowed:
                result[key.lower()] = value[:500]
        return result or None

    @staticmethod
    def _error_code(error: Exception) -> str:
        if isinstance(error, (ValidationError, ConflictError)):
            return error.code
        return f"TELEGRAM_VALIDATION_FAILED:{type(error).__name__}"[:100]

    @staticmethod
    def _required_text(value: object, *, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ValidationError("INVALID_PAYMENT_FIELD", f"{field} must be a non-empty string up to {maximum} characters")
        return value.strip()

    @staticmethod
    def _optional_text(value: object, *, maximum: int) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ValidationError("INVALID_PAYMENT_FIELD", f"Payment field must be a non-empty string up to {maximum} characters")
        return value.strip()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()

    @staticmethod
    def attempt_snapshot(attempt: PaymentAttempt) -> dict[str, object]:
        return canonical_payload(
            {
                "id": attempt.id,
                "order_id": attempt.order_id,
                "buyer_user_id": attempt.buyer_user_id,
                "method": attempt.method,
                "provider_namespace": attempt.provider_namespace,
                "status": attempt.status,
                "order_amount_paise": attempt.order_amount_paise,
                "order_currency": attempt.order_currency,
                "provider_amount": attempt.provider_amount,
                "provider_currency": attempt.provider_currency,
                "merchant_reference": attempt.merchant_reference,
                "provider_order_reference": attempt.provider_order_reference,
                "provider_payment_reference": attempt.provider_payment_reference,
                "telegram_invoice_payload": attempt.telegram_invoice_payload,
                "telegram_user_id": attempt.telegram_user_id,
                "manual_destination_id": attempt.manual_destination_id,
                "method_data_snapshot": attempt.method_data_snapshot,
                "expires_at": PaymentOrchestrator._as_utc(attempt.expires_at).isoformat(),
                "settled_at": PaymentOrchestrator._as_utc(attempt.succeeded_at).isoformat()
                if attempt.succeeded_at
                else None,
                "terminal_reason": attempt.failure_reason,
            }
        )

    @classmethod
    def attempt_mutation_snapshot(cls, *, order: Order, attempt: PaymentAttempt) -> dict[str, object]:
        return canonical_payload({"order": OrderService.snapshot(order), "payment": cls.attempt_snapshot(attempt)})

    @staticmethod
    def manual_destination_snapshot(destination: ManualPaymentDestination) -> dict[str, object]:
        instructions: dict[str, object] = {}
        if destination.instructions:
            try:
                decoded = json.loads(destination.instructions)
            except (TypeError, ValueError):
                decoded = {"text": destination.instructions}
            instructions = decoded if isinstance(decoded, dict) else {"text": destination.instructions}
        return canonical_payload(
            {
                "id": destination.id,
                "display_label": destination.display_label,
                "upi_id": destination.upi_id,
                "qr_reference": destination.qr_reference,
                "instructions": instructions,
                "status": destination.status,
                "approved_by_user_id": destination.approved_by_user_id,
                "approval_evidence_reference": destination.approval_evidence_reference,
                "approved_at": PaymentOrchestrator._as_utc(destination.approved_at).isoformat()
                if destination.approved_at
                else None,
            }
        )

    @staticmethod
    def manual_proof_snapshot(proof: ManualPaymentProof) -> dict[str, object]:
        return canonical_payload(
            {
                "id": proof.id,
                "payment_attempt_id": proof.payment_attempt_id,
                "manual_destination_id": proof.manual_destination_id,
                "buyer_user_id": proof.buyer_user_id,
                "utr": proof.utr,
                "proof_reference": proof.proof_reference,
                "submitted_amount_paise": proof.submitted_amount_paise,
                "submitted_currency": proof.submitted_currency,
                "status": proof.status,
                "reviewed_by_user_id": proof.reviewed_by_user_id,
                "reviewed_at": PaymentOrchestrator._as_utc(proof.reviewed_at).isoformat()
                if proof.reviewed_at
                else None,
                "review_note": proof.review_note,
            }
        )

    @classmethod
    def manual_proof_mutation_snapshot(
        cls,
        order: Order,
        attempt: PaymentAttempt,
        proof: ManualPaymentProof | None,
        *,
        review_required: bool,
        settlement_id: UUID | None = None,
    ) -> dict[str, object]:
        return canonical_payload(
            {
                "order": OrderService.snapshot(order),
                "payment": cls.attempt_snapshot(attempt),
                "proof": cls.manual_proof_snapshot(proof) if proof is not None else None,
                "review_required": review_required,
                "settlement_id": settlement_id,
            }
        )

    @staticmethod
    def settlement_snapshot(settlement: PaymentSettlement) -> dict[str, object]:
        return canonical_payload(
            {
                "id": settlement.id,
                "payment_attempt_id": settlement.payment_attempt_id,
                "order_id": settlement.order_id,
                "journal_group_id": settlement.journal_group_id,
                "amount_paise": settlement.amount_paise,
                "currency": settlement.currency,
                "verification_source": settlement.verification_source,
                "settled_at": PaymentOrchestrator._as_utc(settlement.settled_at).isoformat(),
            }
        )

    @staticmethod
    def provider_event_snapshot(event: PaymentProviderEvent) -> dict[str, object]:
        return canonical_payload(
            {
                "id": event.id,
                "provider_namespace": event.provider_namespace,
                "external_event_id": event.external_event_id,
                "event_type": event.event_type,
                "payload_digest": event.payload_digest,
                "payment_attempt_id": event.payment_attempt_id,
                "merchant_reference": event.merchant_reference,
                "provider_payment_reference": event.provider_payment_reference,
                "telegram_payment_charge_id": event.telegram_payment_charge_id,
                "status": event.status,
                "processing_error": event.processing_error,
            }
        )

    @classmethod
    def provider_event_response(
        cls, event: PaymentProviderEvent, *, settlement_id: UUID | None = None
    ) -> dict[str, object]:
        return canonical_payload(
            {
                "id": event.id,
                "status": event.status,
                "payment_attempt_id": event.payment_attempt_id,
                "settlement_id": settlement_id,
                "review_required": event.status == PaymentProviderEventStatus.REVIEW_REQUIRED,
            }
        )
