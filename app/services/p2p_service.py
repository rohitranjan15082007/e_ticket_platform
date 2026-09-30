"""Server-owned P2P matching, evidence, confirmation and settlement workflow."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.audit import AuditService
from app.core.digests import canonical_payload_digest
from app.core.idempotency import IdempotencyService, canonical_payload, fingerprint
from app.core.money import require_inr_currency, require_paise
from app.core.permissions import RoleName
from app.core.state_machine import (
    ORDER_TRANSITIONS,
    P2P_DISPUTE_TRANSITIONS,
    P2P_MATCH_TRANSITIONS,
    WITHDRAWAL_TRANSITIONS,
    require_transition,
)
from app.core.transaction_locks import lock_key_for_transaction
from app.exceptions import AppError, AuthorizationError, ConflictError, ValidationError
from app.models.order import DeliveryStatus, Order, OrderStatus
from app.models.p2p_match import (
    AdminResolutionDecision,
    OutboxEventStatus,
    P2PAdminResolution,
    P2PDispute,
    P2PDisputeStatus,
    P2PMatch,
    P2PMatchStatus,
    P2POutboxEvent,
    P2PPaymentSubmission,
    P2PReceiverConfirmation,
    P2PSettlement,
    P2PVerifiedPaymentReference,
    PaymentSubmissionVerificationStatus,
    ReceiverConfirmationDecision,
)
from app.models.revenue_allocation import RevenueAllocationSource
from app.models.user import IdempotencyRecord, User
from app.models.wallet import Wallet, WalletHold, WalletHoldStatus
from app.models.withdrawal import PaymentDestination, PaymentDestinationStatus, WithdrawalRequest, WithdrawalStatus
from app.repositories.order_repository import get_order
from app.repositories.payment_repository import (
    get_active_match_for_order,
    get_active_match_for_withdrawal,
    get_dispute_for_match,
    get_match,
    get_receiver_confirmation,
    get_settlement_for_match,
    get_submission,
    get_submission_for_match_reference,
    get_verified_reference,
    list_other_submissions_for_reference,
)
from app.repositories.wallet_repository import get_wallet_by_id, get_wallet_hold_by_reference
from app.repositories.withdrawal_repository import (
    find_oldest_exact_withdrawal,
    get_payment_destination,
    get_withdrawal,
)
from app.services.order_service import OrderService
from app.services.cashback_service import CashbackService
from app.services.coupon_service import CouponService
from app.services.p2p_risk_service import P2PRiskService
from app.services.referral_service import ReferralService
from app.services.revenue_service import RevenueService
from app.services.ticket_allocation_service import TicketAllocationService
from app.services.wallet_service import WalletService
from app.services.withdrawal_service import P2P_WITHDRAWAL_REFERENCE_TYPE


@dataclass(frozen=True, slots=True)
class P2PMatchMutationResult:
    match: P2PMatch | None
    response_payload: dict[str, object]
    replayed: bool


@dataclass(frozen=True, slots=True)
class _MatchContext:
    order: Order
    withdrawal: WithdrawalRequest
    wallet: Wallet
    match: P2PMatch


class P2PService:
    """Apply P2P transitions under explicit locks and durable idempotency records.

    A payment claim never becomes verification here. The only settlement entry
    points are an internal supported-verification path and an admin override
    that persists a reason and independent evidence reference.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.idempotency = IdempotencyService(session)
        self.wallets = WalletService(session)
        self.risk = P2PRiskService(session)

    async def queue_or_match_order(
        self,
        *,
        order_id: UUID,
        buyer_user_id: UUID,
        idempotency_key: str,
        commit: bool,
    ) -> P2PMatchMutationResult:
        """Put an eligible buyer order into FIFO exact matching and expose once assigned."""

        await self.risk.assert_eligible(buyer_user_id)
        order = await self._locked_order(order_id)
        self._assert_buyer(order, buyer_user_id)
        scope = f"user:{buyer_user_id}:p2p.match"
        request_fingerprint = fingerprint({"order_id": order_id, "action": "queue_or_match"})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_match(replay)
        existing = await get_active_match_for_order(self.session, order.id, for_update=True)
        if existing is not None:
            response = self.queue_snapshot(order, existing)
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="p2p_match",
                resource_id=existing.id,
            )
            record.response_payload = response
            await self._finish(commit=commit)
            return P2PMatchMutationResult(existing, response, False)
        if order.status not in {OrderStatus.PENDING_PAYMENT, OrderStatus.WAITING_FOR_MATCH}:
            raise ConflictError("INVALID_TRANSITION", "Order is not available for P2P matching")
        now = datetime.now(timezone.utc)
        if now >= self._as_utc(order.expires_at):
            raise ConflictError("ORDER_EXPIRED", "The unexposed order reservation has expired")
        before_order = OrderService._order_state(order)
        if order.status == OrderStatus.PENDING_PAYMENT:
            require_transition(
                current=order.status,
                target=OrderStatus.WAITING_FOR_MATCH,
                transitions=ORDER_TRANSITIONS,
                resource="Order",
            )
            order.status = OrderStatus.WAITING_FOR_MATCH

        candidate = await find_oldest_exact_withdrawal(
            self.session,
            amount_paise=order.total_paise,
            currency=order.currency,
            excluding_user_id=buyer_user_id,
            for_update=True,
        )
        if candidate is None:
            self.audit.record(
                actor_user_id=buyer_user_id,
                entity_type="order",
                entity_id=order.id,
                action="P2P_ORDER_WAITING_FOR_EXACT_MATCH",
                before_state=before_order,
                after_state=OrderService._order_state(order),
                reason="No eligible exact withdrawal request exists",
            )
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="order",
                resource_id=order.id,
            )
            await self.session.flush()
            response = self.queue_snapshot(order, None)
            record.response_payload = response
            await self._finish(commit=commit)
            return P2PMatchMutationResult(None, response, False)

        # The buyer was checked above. The receiver must remain an active
        # platform user when their frozen destination would be exposed.
        await self.risk.assert_eligible(candidate.user_id)
        destination = await get_payment_destination(
            self.session, candidate.payment_destination_id, for_update=True
        )
        wallet = await get_wallet_by_id(self.session, candidate.wallet_id, for_update=True)
        hold = await get_wallet_hold_by_reference(
            self.session,
            wallet_id=candidate.wallet_id,
            reference_type=P2P_WITHDRAWAL_REFERENCE_TYPE,
            reference_id=candidate.id,
            for_update=True,
        )
        if not self._candidate_is_matchable(candidate, destination, wallet, hold, order):
            before_withdrawal = self.withdrawal_state(candidate)
            require_transition(
                current=candidate.status,
                target=WithdrawalStatus.UNDER_REVIEW,
                transitions=WITHDRAWAL_TRANSITIONS,
                resource="Withdrawal",
            )
            candidate.status = WithdrawalStatus.UNDER_REVIEW
            self.audit.record(
                actor_user_id=None,
                entity_type="withdrawal_request",
                entity_id=candidate.id,
                action="P2P_WITHDRAWAL_REQUIRES_REVIEW",
                before_state=before_withdrawal,
                after_state=self.withdrawal_state(candidate),
                reason="Stored eligibility, hold, wallet, or verified destination was inconsistent at match time",
            )
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="order",
                resource_id=order.id,
            )
            await self.session.flush()
            response = self.queue_snapshot(order, None)
            record.response_payload = response
            await self._finish(commit=commit)
            return P2PMatchMutationResult(None, response, False)
        if await get_active_match_for_withdrawal(self.session, candidate.id, for_update=True) is not None:
            raise ConflictError("WITHDRAWAL_ALREADY_MATCHED", "The exact withdrawal was claimed concurrently")

        payment_deadline = min(
            now + timedelta(minutes=get_settings().p2p_buyer_payment_window_minutes),
            self._as_utc(order.expires_at),
        )
        if payment_deadline <= now:
            raise ConflictError("ORDER_EXPIRED", "Order reservation elapsed before payment instructions could be exposed")
        before_withdrawal = self.withdrawal_state(candidate)
        match = P2PMatch(
            id=uuid4(),
            order_id=order.id,
            withdrawal_id=candidate.id,
            buyer_user_id=buyer_user_id,
            receiver_user_id=candidate.user_id,
            amount_paise=order.total_paise,
            currency=order.currency,
            destination_snapshot=self._frozen_destination_snapshot(destination),
            status=P2PMatchStatus.WAITING_FOR_PAYMENT,
            payment_deadline_at=payment_deadline,
            instructions_exposed_at=now,
        )
        self.session.add(match)
        require_transition(
            current=candidate.status,
            target=WithdrawalStatus.MATCHED,
            transitions=WITHDRAWAL_TRANSITIONS,
            resource="Withdrawal",
        )
        candidate.status = WithdrawalStatus.MATCHED
        candidate.matched_at = now
        require_transition(
            current=order.status,
            target=OrderStatus.AWAITING_PAYMENT,
            transitions=ORDER_TRANSITIONS,
            resource="Order",
        )
        order.status = OrderStatus.AWAITING_PAYMENT
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_match",
            resource_id=match.id,
        )
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="order",
            entity_id=order.id,
            action="P2P_ORDER_MATCHED",
            before_state=before_order,
            after_state=OrderService._order_state(order),
        )
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="withdrawal_request",
            entity_id=candidate.id,
            action="P2P_WITHDRAWAL_MATCHED",
            before_state=before_withdrawal,
            after_state=self.withdrawal_state(candidate),
        )
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="p2p_match",
            entity_id=match.id,
            action="P2P_PAYMENT_INSTRUCTIONS_EXPOSED",
            before_state=None,
            after_state=self.match_state(match),
            reason="Exact FIFO withdrawal match claimed under locks",
        )
        self._enqueue_notification(
            aggregate_type="p2p_match",
            aggregate_id=match.id,
            notification_type="P2P_MATCH_ASSIGNED",
            recipient_user_ids=[match.buyer_user_id, match.receiver_user_id],
            payload={"match_id": match.id, "amount_paise": match.amount_paise, "currency": match.currency},
            deduplication_key=f"p2p-match:{match.id}:notification:assigned",
            actor_user_id=buyer_user_id,
        )
        await self.session.flush()
        response = self.queue_snapshot(order, match)
        record.response_payload = response
        await self._finish(commit=commit)
        return P2PMatchMutationResult(match, response, False)

    async def get_match_snapshot_for_actor(
        self, *, match_id: UUID, actor_user_id: UUID
    ) -> dict[str, object]:
        match = await get_match(self.session, match_id)
        if match is None:
            raise ValidationError("UNKNOWN_MATCH", "P2P match does not exist")
        actor = await self._active_user(actor_user_id)
        is_admin = RoleName.ADMIN.value in {role.name for role in actor.roles}
        if actor_user_id not in {match.buyer_user_id, match.receiver_user_id} and not is_admin:
            raise AuthorizationError("You cannot view this payment attempt")
        return self.match_snapshot(match, include_destination=True)

    async def submit_payment(
        self,
        *,
        match_id: UUID,
        buyer_user_id: UUID,
        provider_namespace: str,
        claimed_reference: str,
        observed_amount_paise: int,
        observed_currency: str,
        declared_paid_at: datetime,
        evidence_upload_id: UUID | None,
        provider_evidence_reference: str | None,
        evidence_metadata: dict[str, object] | None,
        idempotency_key: str,
        commit: bool,
    ) -> P2PMatchMutationResult:
        """Persist a buyer payment claim without treating it as verification."""

        provider_namespace = self._reference_text(provider_namespace, field="provider_namespace", limit=64)
        claimed_reference = self._reference_text(claimed_reference, field="claimed_reference", limit=160)
        observed_amount = require_paise(observed_amount_paise, field="observed_amount_paise")
        observed_currency = require_inr_currency(observed_currency)
        declared_paid_at = self._as_utc(declared_paid_at)
        if evidence_upload_id is not None and not isinstance(evidence_upload_id, UUID):
            raise ValidationError("INVALID_EVIDENCE", "evidence_upload_id must be a UUID")
        provider_evidence_reference = self._optional_text(
            provider_evidence_reference, field="provider_evidence_reference", limit=255
        )
        safe_metadata = canonical_payload(evidence_metadata or {})
        initial = await get_match(self.session, match_id)
        if initial is None:
            raise ValidationError("UNKNOWN_MATCH", "P2P match does not exist")
        if initial.buyer_user_id != buyer_user_id:
            raise AuthorizationError("Only the assigned buyer can submit payment evidence")
        await self.risk.assert_eligible(buyer_user_id)
        context = await self._locked_match_context(match_id)
        # A row lock cannot protect a reference that has not been claimed yet.
        # Serialize all same-reference claims until their surrounding
        # transaction commits so a concurrent cross-match reuse is immediately
        # routed to review rather than both claims advancing independently.
        await self._lock_payment_reference(provider_namespace, claimed_reference)
        scope = f"user:{buyer_user_id}:p2p.payment-submission"
        request_fingerprint = fingerprint(
            {
                "match_id": match_id,
                "provider_namespace": provider_namespace,
                "claimed_reference": claimed_reference,
                "observed_amount_paise": observed_amount,
                "observed_currency": observed_currency,
                "declared_paid_at": declared_paid_at,
                "evidence_upload_id": evidence_upload_id,
                "provider_evidence_reference": provider_evidence_reference,
                "evidence_metadata": safe_metadata,
            }
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_match(replay)
        match = context.match
        existing = await get_submission_for_match_reference(
            self.session,
            match_id=match.id,
            provider_namespace=provider_namespace,
            claimed_reference=claimed_reference,
            for_update=True,
        )
        if existing is not None:
            response = self.submission_snapshot(match, existing)
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="p2p_payment_submission",
                resource_id=existing.id,
            )
            record.response_payload = response
            await self._finish(commit=commit)
            return P2PMatchMutationResult(match, response, False)

        now = datetime.now(timezone.utc)
        terminal = match.status in {P2PMatchStatus.SETTLED, P2PMatchStatus.CLOSED_UNPAID, P2PMatchStatus.REFUNDED}
        is_late = terminal or now > self._as_utc(match.payment_deadline_at)
        if match.status not in {
            P2PMatchStatus.WAITING_FOR_PAYMENT,
            P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION,
            P2PMatchStatus.UNDER_VERIFICATION,
            P2PMatchStatus.DISPUTED,
            P2PMatchStatus.CLOSED_UNPAID,
            P2PMatchStatus.SETTLED,
        }:
            raise ConflictError("INVALID_TRANSITION", "Payment evidence cannot be submitted in this match state")
        cross_match_claims = await list_other_submissions_for_reference(
            self.session,
            match_id=match.id,
            provider_namespace=provider_namespace,
            claimed_reference=claimed_reference,
            for_update=True,
        )
        has_conflict = bool(cross_match_claims)
        amount_matches = observed_amount == match.amount_paise and observed_currency == match.currency
        submission = P2PPaymentSubmission(
            id=uuid4(),
            match_id=match.id,
            buyer_user_id=buyer_user_id,
            provider_namespace=provider_namespace,
            claimed_reference=claimed_reference,
            expected_amount_paise=match.amount_paise,
            expected_currency=match.currency,
            observed_amount_paise=observed_amount,
            observed_currency=observed_currency,
            declared_paid_at=declared_paid_at,
            evidence_upload_id=evidence_upload_id,
            provider_evidence_reference=provider_evidence_reference,
            evidence_metadata=safe_metadata or None,
            verification_status=(
                PaymentSubmissionVerificationStatus.MANUAL_REVIEW
                if has_conflict or not amount_matches or is_late
                else PaymentSubmissionVerificationStatus.UNVERIFIED
            ),
            verification_reason=(
                "REFERENCE_CONFLICT" if has_conflict else "AMOUNT_MISMATCH" if not amount_matches else
                "LATE_PAYMENT_CLAIM" if is_late else None
            ),
            is_late=is_late,
        )
        self.session.add(submission)
        before_match = self.match_state(match)
        if not terminal:
            if match.status == P2PMatchStatus.WAITING_FOR_PAYMENT:
                if is_late:
                    self._transition_match(match, P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION)
                    match.instructions_disabled_at = now
                else:
                    self._transition_match(match, P2PMatchStatus.PAYMENT_SUBMITTED)
            if match.status == P2PMatchStatus.PAYMENT_SUBMITTED:
                self._transition_match(
                    match,
                    P2PMatchStatus.UNDER_VERIFICATION
                    if has_conflict or not amount_matches
                    else P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION,
                )
            elif match.status == P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION:
                self._transition_match(match, P2PMatchStatus.UNDER_VERIFICATION)
            elif match.status == P2PMatchStatus.DISPUTED:
                self._transition_match(match, P2PMatchStatus.ADMIN_REVIEW)
            if match.status in {
                P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION,
                P2PMatchStatus.UNDER_VERIFICATION,
            }:
                match.receiver_confirmation_deadline_at = now + timedelta(
                    minutes=get_settings().p2p_receiver_confirmation_window_minutes
                )
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_payment_submission",
            resource_id=submission.id,
        )
        self.audit.record(
            actor_user_id=buyer_user_id,
            entity_type="p2p_payment_submission",
            entity_id=submission.id,
            action="P2P_PAYMENT_CLAIM_SUBMITTED",
            before_state=None,
            after_state=self.submission_state(submission),
            reason="A submitted UTR/proof remains an unverified claim",
        )
        if not terminal:
            self.audit.record(
                actor_user_id=buyer_user_id,
                entity_type="p2p_match",
                entity_id=match.id,
                action="P2P_MATCH_EVIDENCE_RECORDED",
                before_state=before_match,
                after_state=self.match_state(match),
                reason=submission.verification_reason,
            )
        self._enqueue_notification(
            aggregate_type="p2p_match",
            aggregate_id=match.id,
            notification_type="P2P_PAYMENT_SUBMISSION_RECORDED",
            recipient_user_ids=[match.buyer_user_id, match.receiver_user_id],
            payload={
                "match_id": match.id,
                "submission_id": submission.id,
                "review_required": submission.verification_status == PaymentSubmissionVerificationStatus.MANUAL_REVIEW,
            },
            deduplication_key=f"p2p-submission:{submission.id}:notification:recorded",
            actor_user_id=buyer_user_id,
        )
        await self.session.flush()
        response = self.submission_snapshot(match, submission)
        record.response_payload = response
        await self._finish(commit=commit)
        return P2PMatchMutationResult(match, response, False)

    async def confirm_receipt(
        self,
        *,
        match_id: UUID,
        receiver_user_id: UUID,
        decision: ReceiverConfirmationDecision,
        reason: str | None,
        idempotency_key: str,
        commit: bool,
    ) -> P2PMatchMutationResult:
        """Record receiver acknowledgement; it never by itself settles a payment."""

        if not isinstance(decision, ReceiverConfirmationDecision):
            raise ValidationError("INVALID_CONFIRMATION", "Receiver decision is invalid")
        reason = self._optional_text(reason, field="reason", limit=500)
        initial = await get_match(self.session, match_id)
        if initial is None:
            raise ValidationError("UNKNOWN_MATCH", "P2P match does not exist")
        if initial.receiver_user_id != receiver_user_id:
            raise AuthorizationError("Only the assigned receiver can confirm this payment")
        await self.risk.assert_eligible(receiver_user_id)
        context = await self._locked_match_context(match_id)
        match = context.match
        scope = f"user:{receiver_user_id}:p2p.receiver-confirmation"
        request_fingerprint = fingerprint({"match_id": match_id, "decision": decision, "reason": reason})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_match(replay)
        prior = await get_receiver_confirmation(self.session, match.id, for_update=True)
        if prior is not None:
            if prior.decision != decision:
                raise ConflictError("INVALID_TRANSITION", "Receiver confirmation is immutable once recorded")
            response = self.confirmation_snapshot(match, prior)
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="p2p_receiver_confirmation",
                resource_id=prior.id,
            )
            record.response_payload = response
            await self._finish(commit=commit)
            return P2PMatchMutationResult(match, response, False)
        submission = await self.session.scalar(
            select(P2PPaymentSubmission)
            .where(P2PPaymentSubmission.match_id == match.id)
            .order_by(P2PPaymentSubmission.created_at.desc(), P2PPaymentSubmission.id.desc())
            .with_for_update()
        )
        if submission is None:
            raise ConflictError("PAYMENT_PROOF_REQUIRED", "A receiver cannot confirm before a payment claim exists")
        if match.status not in {
            P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION,
            P2PMatchStatus.UNDER_VERIFICATION,
            P2PMatchStatus.DISPUTED,
        }:
            raise ConflictError("INVALID_TRANSITION", "Receiver confirmation is not expected in this match state")
        now = datetime.now(timezone.utc)
        before_match = self.match_state(match)
        before_withdrawal = self.withdrawal_state(context.withdrawal)
        confirmation = P2PReceiverConfirmation(
            id=uuid4(),
            match_id=match.id,
            receiver_user_id=receiver_user_id,
            decision=decision,
            reason=reason,
            confirmed_at=now,
        )
        self.session.add(confirmation)
        dispute: P2PDispute | None = None
        created_dispute = False
        if decision == ReceiverConfirmationDecision.RECEIVED:
            if match.status == P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION:
                self._transition_match(match, P2PMatchStatus.UNDER_VERIFICATION)
            # A signed provider event is evaluated independently. This response
            # stays under verification until that evidence (or an admin
            # override) crosses the settlement boundary.
        else:
            if match.status != P2PMatchStatus.DISPUTED:
                self._transition_match(match, P2PMatchStatus.DISPUTED)
            if context.withdrawal.status == WithdrawalStatus.MATCHED:
                self._transition_withdrawal(context.withdrawal, WithdrawalStatus.UNDER_REVIEW)
            dispute = await get_dispute_for_match(self.session, match.id, for_update=True)
            if dispute is None:
                dispute = P2PDispute(
                    id=uuid4(),
                    match_id=match.id,
                    opened_by_user_id=receiver_user_id,
                    status=P2PDisputeStatus.OPEN,
                    reason_code="RECEIVER_DENIED_RECEIPT",
                    evidence_reference=None,
                )
                self.session.add(dispute)
                created_dispute = True
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_receiver_confirmation",
            resource_id=confirmation.id,
        )
        self.audit.record(
            actor_user_id=receiver_user_id,
            entity_type="p2p_receiver_confirmation",
            entity_id=confirmation.id,
            action="P2P_RECEIVER_CONFIRMATION_RECORDED",
            before_state=None,
            after_state=self.confirmation_state(confirmation),
        )
        self.audit.record(
            actor_user_id=receiver_user_id,
            entity_type="p2p_match",
            entity_id=match.id,
            action="P2P_RECEIVER_DECISION_APPLIED",
            before_state=before_match,
            after_state=self.match_state(match),
            reason=reason,
        )
        if context.withdrawal.status != WithdrawalStatus.MATCHED:
            self.audit.record(
                actor_user_id=receiver_user_id,
                entity_type="withdrawal_request",
                entity_id=context.withdrawal.id,
                action="P2P_WITHDRAWAL_REQUIRES_REVIEW",
                before_state=before_withdrawal,
                after_state=self.withdrawal_state(context.withdrawal),
                reason="Receiver denied receipt",
            )
        if dispute is not None and created_dispute:
            self.audit.record(
                actor_user_id=receiver_user_id,
                entity_type="p2p_dispute",
                entity_id=dispute.id,
                action="P2P_DISPUTE_OPENED",
                before_state=None,
                after_state=self.dispute_state(dispute),
                reason="Receiver denied receipt",
            )
        self._enqueue_notification(
            aggregate_type="p2p_match",
            aggregate_id=match.id,
            notification_type="P2P_RECEIVER_CONFIRMATION_RECORDED",
            recipient_user_ids=[match.buyer_user_id, match.receiver_user_id],
            payload={
                "match_id": match.id,
                "confirmation_id": confirmation.id,
                "decision": confirmation.decision,
            },
            deduplication_key=f"p2p-confirmation:{confirmation.id}:notification:recorded",
            actor_user_id=receiver_user_id,
        )
        if (
            decision == ReceiverConfirmationDecision.RECEIVED
            and submission.verification_status == PaymentSubmissionVerificationStatus.VERIFIED
        ):
            settlement_event = P2POutboxEvent(
                id=uuid4(),
                aggregate_type="p2p_match",
                aggregate_id=match.id,
                event_type="P2P_SUPPORTED_SETTLEMENT_REQUESTED",
                deduplication_key=f"p2p-match:{match.id}:supported-settlement",
                payload=canonical_payload(
                    {
                        "match_id": match.id,
                        "payment_submission_id": submission.id,
                        "verification_source": "PROVIDER_OUTBOX",
                    }
                ),
                status=OutboxEventStatus.PENDING,
            )
            self.session.add(settlement_event)
            self.audit.record(
                actor_user_id=receiver_user_id,
                entity_type="p2p_outbox_event",
                entity_id=settlement_event.id,
                action="P2P_SUPPORTED_SETTLEMENT_OUTBOX_ENQUEUED",
                before_state=None,
                after_state=self.outbox_state(settlement_event),
                reason="Provider verification was already present when receiver confirmed receipt",
            )
        await self.session.flush()
        response = self.confirmation_snapshot(match, confirmation)
        record.response_payload = response
        await self._finish(commit=commit)
        return P2PMatchMutationResult(match, response, False)

    async def expire_match(
        self, *, match_id: UUID, idempotency_key: str, commit: bool
    ) -> P2PMatchMutationResult:
        """Durably move expired attempts into reconciliation; never release their holds."""

        context = await self._locked_match_context(match_id)
        match = context.match
        scope = "system:p2p.match-expiry"
        request_fingerprint = fingerprint({"match_id": match_id, "action": "expire"})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_match(replay)
        now = datetime.now(timezone.utc)
        before = self.match_state(match)
        action: str | None = None
        if match.status == P2PMatchStatus.WAITING_FOR_PAYMENT and now >= self._as_utc(match.payment_deadline_at):
            self._transition_match(match, P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION)
            match.instructions_disabled_at = now
            action = "P2P_PAYMENT_WINDOW_EXPIRED_RECONCILIATION_REQUIRED"
        elif (
            match.status == P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION
            and match.receiver_confirmation_deadline_at is not None
            and now >= self._as_utc(match.receiver_confirmation_deadline_at)
        ):
            self._transition_match(match, P2PMatchStatus.UNDER_VERIFICATION)
            match.instructions_disabled_at = now
            action = "P2P_RECEIVER_TIMEOUT_REQUIRES_REVIEW"
        else:
            raise ConflictError("MATCH_NOT_EXPIRED", "Match does not currently require expiry handling")
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_match",
            resource_id=match.id,
        )
        self.audit.record(
            actor_user_id=None,
            entity_type="p2p_match",
            entity_id=match.id,
            action=action,
            before_state=before,
            after_state=self.match_state(match),
            reason="No automatic approval, rejection, release, or rematch follows a timeout",
        )
        self._enqueue_notification(
            aggregate_type="p2p_match",
            aggregate_id=match.id,
            notification_type="P2P_RECONCILIATION_REQUIRED",
            recipient_user_ids=[match.buyer_user_id, match.receiver_user_id],
            payload={"match_id": match.id, "reason_code": action},
            deduplication_key=f"p2p-match:{match.id}:notification:{action}",
            actor_user_id=None,
        )
        await self.session.flush()
        response = self.match_snapshot(match, include_destination=False)
        record.response_payload = response
        await self._finish(commit=commit)
        return P2PMatchMutationResult(match, response, False)

    async def settle_supported_verified_payment(
        self,
        *,
        match_id: UUID,
        actor_user_id: UUID | None,
        payment_submission_id: UUID,
        verification_source: str,
        idempotency_key: str,
        commit: bool,
    ) -> P2PMatchMutationResult:
        """Internal trusted-evidence path requiring verification and receipt confirmation."""

        verification_source = self._reference_text(verification_source, field="verification_source", limit=100)
        context = await self._locked_match_context(match_id)
        submission = await get_submission(self.session, payment_submission_id, for_update=True)
        if submission is None or submission.match_id != context.match.id:
            raise ValidationError("UNKNOWN_PAYMENT_SUBMISSION", "Payment submission does not belong to this match")
        confirmation = await get_receiver_confirmation(self.session, context.match.id, for_update=True)
        if (
            submission.verification_status != PaymentSubmissionVerificationStatus.VERIFIED
            or confirmation is None
            or confirmation.decision != ReceiverConfirmationDecision.RECEIVED
        ):
            raise ConflictError(
                "REVIEW_REQUIRED",
                "Normal settlement requires independently verified payment evidence and receiver confirmation",
            )
        return await self._settle_locked(
            context=context,
            submission=submission,
            actor_user_id=actor_user_id,
            scope="system:p2p.supported-settlement",
            idempotency_key=idempotency_key,
            verification_source=verification_source,
            admin_resolution_data=None,
            commit=commit,
        )

    async def apply_trusted_provider_verification(
        self,
        *,
        match_id: UUID,
        payment_submission_id: UUID,
        provider_namespace: str,
        transaction_reference: str,
        verified_amount_paise: int,
        verified_currency: str,
        recipient_fingerprint: str,
        provider_event_id: str,
        verification_source: str,
        commit: bool,
    ) -> P2PMatchMutationResult:
        """Apply only an already-authenticated provider event to a P2P claim.

        The public webhook service verifies and durably records the callback
        before entering this method.  This method still cross-checks every
        authoritative fact against the immutable match/submission data so an
        adapter cannot turn a generic or wrongly-directed payment into a
        settlement.
        """

        provider_namespace = self._reference_text(provider_namespace, field="provider_namespace", limit=64)
        transaction_reference = self._reference_text(
            transaction_reference, field="transaction_reference", limit=160
        )
        provider_event_id = self._reference_text(provider_event_id, field="provider_event_id", limit=160)
        if len(provider_event_id) < 8:
            raise ValidationError("INVALID_PROVIDER_EVENT", "provider_event_id must be at least 8 characters")
        verified_amount = require_paise(verified_amount_paise, field="verified_amount_paise")
        verified_currency = require_inr_currency(verified_currency)
        recipient_fingerprint = self._digest_text(recipient_fingerprint, field="recipient_fingerprint")
        verification_source = self._reference_text(verification_source, field="verification_source", limit=100)

        context = await self._locked_match_context(match_id)
        submission = await get_submission(self.session, payment_submission_id, for_update=True)
        if submission is None or submission.match_id != context.match.id:
            raise ValidationError("UNKNOWN_PAYMENT_SUBMISSION", "Payment submission does not belong to this match")
        await self._lock_payment_reference(provider_namespace, transaction_reference)
        scope = f"provider:{provider_namespace}:p2p.verification"
        request_fingerprint = fingerprint(
            {
                "provider_event_id": provider_event_id,
                "match_id": match_id,
                "payment_submission_id": payment_submission_id,
                "provider_namespace": provider_namespace,
                "transaction_reference": transaction_reference,
                "verified_amount_paise": verified_amount,
                "verified_currency": verified_currency,
                "recipient_fingerprint": recipient_fingerprint,
                "verification_source": verification_source,
            }
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=provider_event_id, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_match(replay)

        match = context.match
        if match.status in {
            P2PMatchStatus.SETTLED,
            P2PMatchStatus.CLOSED_UNPAID,
            P2PMatchStatus.REFUND_PENDING,
            P2PMatchStatus.REFUNDED,
        }:
            return await self._record_provider_review(
                context=context,
                submission=submission,
                scope=scope,
                idempotency_key=provider_event_id,
                request_fingerprint=request_fingerprint,
                reason_code="LATE_PROVIDER_EVENT",
                reason="A provider event arrived after the original P2P attempt was no longer active",
                commit=commit,
            )
        if (
            submission.provider_namespace != provider_namespace
            or submission.claimed_reference != transaction_reference
        ):
            return await self._record_provider_review(
                context=context,
                submission=submission,
                scope=scope,
                idempotency_key=provider_event_id,
                request_fingerprint=request_fingerprint,
                reason_code="PROVIDER_REFERENCE_MISMATCH",
                reason="Provider event does not match the buyer's submitted namespace/reference",
                commit=commit,
            )
        if (
            verified_amount != match.amount_paise
            or verified_currency != match.currency
            or submission.observed_amount_paise != match.amount_paise
            or submission.observed_currency != match.currency
        ):
            return await self._record_provider_review(
                context=context,
                submission=submission,
                scope=scope,
                idempotency_key=provider_event_id,
                request_fingerprint=request_fingerprint,
                reason_code="AMOUNT_MISMATCH",
                reason="Provider or submitted amount/currency does not equal the immutable match amount",
                commit=commit,
            )
        if recipient_fingerprint != self._recipient_fingerprint(match):
            return await self._record_provider_review(
                context=context,
                submission=submission,
                scope=scope,
                idempotency_key=provider_event_id,
                request_fingerprint=request_fingerprint,
                reason_code="RECIPIENT_MISMATCH",
                reason="Provider event is not cryptographically tied to the frozen recipient snapshot",
                commit=commit,
            )
        if await list_other_submissions_for_reference(
            self.session,
            match_id=match.id,
            provider_namespace=provider_namespace,
            claimed_reference=transaction_reference,
            for_update=True,
        ):
            return await self._record_reference_conflict(
                context=context,
                submission=submission,
                actor_user_id=None,
                scope=scope,
                idempotency_key=provider_event_id,
                request_fingerprint=request_fingerprint,
                commit=commit,
            )
        verified_reference = await get_verified_reference(
            self.session,
            provider_namespace=provider_namespace,
            transaction_reference=transaction_reference,
            for_update=True,
        )
        if verified_reference is not None and (
            verified_reference.match_id != match.id
            or verified_reference.payment_submission_id != submission.id
        ):
            return await self._record_reference_conflict(
                context=context,
                submission=submission,
                actor_user_id=None,
                scope=scope,
                idempotency_key=provider_event_id,
                request_fingerprint=request_fingerprint,
                commit=commit,
            )
        if verified_reference is None:
            verified_reference = P2PVerifiedPaymentReference(
                id=uuid4(),
                payment_submission_id=submission.id,
                match_id=match.id,
                provider_namespace=provider_namespace,
                transaction_reference=transaction_reference,
                verification_source=verification_source,
            )
            self.session.add(verified_reference)
        before_submission = self.submission_state(submission)
        submission.verification_status = PaymentSubmissionVerificationStatus.VERIFIED
        submission.verification_reason = None
        submission.verified_at = datetime.now(timezone.utc)
        self.audit.record(
            actor_user_id=None,
            entity_type="p2p_payment_submission",
            entity_id=submission.id,
            action="P2P_PROVIDER_PAYMENT_VERIFIED",
            before_state=before_submission,
            after_state=self.submission_state(submission),
            reason=verification_source,
        )
        confirmation = await get_receiver_confirmation(self.session, match.id, for_update=True)
        if confirmation is not None and confirmation.decision == ReceiverConfirmationDecision.RECEIVED:
            return await self._settle_locked(
                context=context,
                submission=submission,
                actor_user_id=None,
                scope=f"provider:{provider_namespace}:p2p.settlement",
                idempotency_key=provider_event_id,
                verification_source=verification_source,
                admin_resolution_data=None,
                commit=commit,
            )
        record = self.idempotency.record(
            actor_scope=scope,
            key=provider_event_id,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_payment_submission",
            resource_id=submission.id,
        )
        await self.session.flush()
        response = canonical_payload(
            {
                **self.submission_snapshot(match, submission),
                "verification_pending_confirmation": True,
            }
        )
        record.response_payload = response
        await self._finish(commit=commit)
        return P2PMatchMutationResult(match, response, False)

    async def admin_settle(
        self,
        *,
        match_id: UUID,
        actor_user_id: UUID,
        payment_submission_id: UUID,
        reason: str,
        evidence_reference: str,
        verification_source: str,
        dispute_id: UUID | None,
        idempotency_key: str,
        commit: bool,
    ) -> P2PMatchMutationResult:
        """Settle only as an explicitly evidenced, audited admin override."""

        await self._require_admin(actor_user_id)
        if get_settings().p2p_high_risk_maker_checker_required:
            raise ConflictError(
                "MAKER_CHECKER_REQUIRED",
                "This environment requires a separate maker-checker approval before an override",
            )
        reason = self._reference_text(reason, field="reason", limit=500)
        evidence_reference = self._reference_text(evidence_reference, field="evidence_reference", limit=255)
        verification_source = self._reference_text(verification_source, field="verification_source", limit=100)
        context = await self._locked_match_context(match_id)
        submission = await get_submission(self.session, payment_submission_id, for_update=True)
        if submission is None or submission.match_id != context.match.id:
            raise ValidationError("UNKNOWN_PAYMENT_SUBMISSION", "Payment submission does not belong to this match")
        if submission.observed_amount_paise != context.match.amount_paise or submission.observed_currency != "INR":
            raise ConflictError("AMOUNT_MISMATCH", "An amount-mismatched claim cannot be settled")
        if dispute_id is not None:
            dispute = await get_dispute_for_match(self.session, context.match.id, for_update=True)
            if dispute is None or dispute.id != dispute_id:
                raise ValidationError("UNKNOWN_DISPUTE", "Dispute does not belong to this match")
            if dispute.status == P2PDisputeStatus.RESOLVED:
                raise ConflictError("INVALID_TRANSITION", "P2P dispute is already resolved")
        else:
            dispute = await get_dispute_for_match(self.session, context.match.id, for_update=True)
        return await self._settle_locked(
            context=context,
            submission=submission,
            actor_user_id=actor_user_id,
            scope=f"admin:{actor_user_id}:p2p.admin-settlement",
            idempotency_key=idempotency_key,
            verification_source=verification_source,
            admin_resolution_data={
                "reason": reason,
                "evidence_reference": evidence_reference,
                "dispute_id": dispute.id if dispute is not None else None,
            },
            commit=commit,
        )

    async def _settle_locked(
        self,
        *,
        context: _MatchContext,
        submission: P2PPaymentSubmission,
        actor_user_id: UUID | None,
        scope: str,
        idempotency_key: str,
        verification_source: str,
        admin_resolution_data: dict[str, object] | None,
        commit: bool,
    ) -> P2PMatchMutationResult:
        match = context.match
        request_fingerprint = fingerprint(
            {
                "match_id": match.id,
                "payment_submission_id": submission.id,
                "verification_source": verification_source,
                "admin_resolution": admin_resolution_data,
            }
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_match(replay)
        existing = await get_settlement_for_match(self.session, match.id, for_update=True)
        if existing is not None:
            response = self.settlement_snapshot(match, context.order, existing)
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="p2p_settlement",
                resource_id=existing.id,
            )
            record.response_payload = response
            await self._finish(commit=commit)
            return P2PMatchMutationResult(match, response, False)
        if match.status in {P2PMatchStatus.CLOSED_UNPAID, P2PMatchStatus.REFUNDED}:
            raise ConflictError("INVALID_TRANSITION", "A closed P2P attempt cannot be settled automatically")
        if context.order.status != OrderStatus.AWAITING_PAYMENT:
            raise ConflictError("ORDER_NOT_AWAITING_PAYMENT", "Order is not eligible for a P2P settlement")
        if context.withdrawal.status not in {WithdrawalStatus.MATCHED, WithdrawalStatus.UNDER_REVIEW}:
            raise ConflictError("WITHDRAWAL_NOT_MATCHED", "Withdrawal is not eligible for P2P settlement")
        if submission.observed_amount_paise != match.amount_paise or submission.observed_currency != match.currency:
            raise ConflictError("AMOUNT_MISMATCH", "Settlement requires the exact matched amount and currency")
        await self._lock_payment_reference(submission.provider_namespace, submission.claimed_reference)
        verified_reference = await get_verified_reference(
            self.session,
            provider_namespace=submission.provider_namespace,
            transaction_reference=submission.claimed_reference,
            for_update=True,
        )
        if verified_reference is not None and (
            verified_reference.match_id != match.id
            or verified_reference.payment_submission_id != submission.id
        ):
            return await self._record_reference_conflict(
                context=context,
                submission=submission,
                actor_user_id=actor_user_id,
                scope=scope,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                commit=commit,
            )
        settlement_id = uuid4()
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_settlement",
            resource_id=settlement_id,
        )
        await self.session.flush()
        admin_resolution: P2PAdminResolution | None = None
        if admin_resolution_data is not None:
            reason = self._reference_text(
                admin_resolution_data.get("reason"), field="reason", limit=500
            )
            evidence_reference = self._reference_text(
                admin_resolution_data.get("evidence_reference"), field="evidence_reference", limit=255
            )
            dispute_id_value = admin_resolution_data.get("dispute_id")
            if dispute_id_value is not None and not isinstance(dispute_id_value, UUID):
                raise ValidationError("UNKNOWN_DISPUTE", "Dispute identifier is invalid")
            admin_resolution = P2PAdminResolution(
                id=uuid4(),
                match_id=match.id,
                dispute_id=dispute_id_value,
                actor_user_id=actor_user_id,
                decision=AdminResolutionDecision.SETTLE,
                reason=reason,
                evidence_reference=evidence_reference,
                verification_source=verification_source,
            )
            self.session.add(admin_resolution)
            dispute_for_resolution = await get_dispute_for_match(self.session, match.id, for_update=True)
            if (
                dispute_for_resolution is not None
                and dispute_for_resolution.status != P2PDisputeStatus.RESOLVED
            ):
                before_dispute = self.dispute_state(dispute_for_resolution)
                self._transition_dispute(dispute_for_resolution, P2PDisputeStatus.RESOLVED)
                dispute_for_resolution.resolved_at = datetime.now(timezone.utc)
            else:
                before_dispute = None
        else:
            dispute_for_resolution = None
            before_dispute = None
        if verified_reference is None:
            verified_reference = P2PVerifiedPaymentReference(
                id=uuid4(),
                payment_submission_id=submission.id,
                match_id=match.id,
                provider_namespace=submission.provider_namespace,
                transaction_reference=submission.claimed_reference,
                verification_source=verification_source,
            )
            self.session.add(verified_reference)
        before_submission = self.submission_state(submission)
        submission.verification_status = PaymentSubmissionVerificationStatus.VERIFIED
        submission.verification_reason = None
        submission.verified_at = datetime.now(timezone.utc)
        before_match = self.match_state(match)
        before_withdrawal = self.withdrawal_state(context.withdrawal)
        before_order = OrderService._order_state(context.order)
        wallet_result = await self.wallets.settle_active_hold_for_p2p(
            wallet_id=context.wallet.id,
            amount_paise=match.amount_paise,
            reference_type=P2P_WITHDRAWAL_REFERENCE_TYPE,
            reference_id=context.withdrawal.id,
            settlement_id=settlement_id,
            idempotency_record_id=record.id,
            actor_user_id=actor_user_id,
            reason="Verified P2P settlement" if admin_resolution is None else admin_resolution.reason,
            commit=False,
        )
        settlement = P2PSettlement(
            id=settlement_id,
            match_id=match.id,
            withdrawal_id=context.withdrawal.id,
            order_id=context.order.id,
            journal_group_id=wallet_result.journal_group.id,
            idempotency_record_id=record.id,
            amount_paise=match.amount_paise,
            currency=match.currency,
            verification_source=verification_source,
            settled_by_user_id=actor_user_id,
            settled_at=datetime.now(timezone.utc),
        )
        self.session.add(settlement)
        if match.status != P2PMatchStatus.ADMIN_REVIEW:
            self._move_match_to_admin_review(match)
        self._transition_match(match, P2PMatchStatus.SETTLED)
        match.settled_at = settlement.settled_at
        self._transition_withdrawal(context.withdrawal, WithdrawalStatus.COMPLETED)
        context.withdrawal.completed_at = settlement.settled_at
        require_transition(
            current=context.order.status,
            target=OrderStatus.PAID,
            transitions=ORDER_TRANSITIONS,
            resource="Order",
        )
        context.order.status = OrderStatus.PAID
        context.order.delivery_status = DeliveryStatus.PENDING
        context.order.settlement_reference_id = settlement.id
        context.order.settled_at = settlement.settled_at
        await CouponService(self.session).consume_for_settled_order(
            order=context.order, actor_user_id=actor_user_id
        )
        await RevenueService(self.session).allocate_settled_order(
            order=context.order,
            settlement_reference_id=settlement.id,
            source=RevenueAllocationSource.P2P_ORDER_PENDING,
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
        outbox = P2POutboxEvent(
            id=uuid4(),
            aggregate_type="order",
            aggregate_id=context.order.id,
            event_type="P2P_ORDER_DELIVERY_REQUESTED",
            deduplication_key=f"p2p-settlement:{settlement.id}:delivery",
            payload=canonical_payload(
                {"order_id": context.order.id, "settlement_id": settlement.id, "amount_paise": match.amount_paise}
            ),
            status=OutboxEventStatus.PENDING,
        )
        self.session.add(outbox)
        if admin_resolution is not None:
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="p2p_admin_resolution",
                entity_id=admin_resolution.id,
                action="P2P_ADMIN_OVERRIDE_SETTLEMENT_RECORDED",
                before_state=None,
                after_state=self.admin_resolution_state(admin_resolution),
                reason=admin_resolution.reason,
            )
        if dispute_for_resolution is not None and before_dispute is not None:
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="p2p_dispute",
                entity_id=dispute_for_resolution.id,
                action="P2P_DISPUTE_RESOLVED_BY_SETTLEMENT",
                before_state=before_dispute,
                after_state=self.dispute_state(dispute_for_resolution),
                reason=verification_source,
            )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_payment_submission",
            entity_id=submission.id,
            action="P2P_PAYMENT_SUBMISSION_VERIFIED",
            before_state=before_submission,
            after_state=self.submission_state(submission),
            reason=verification_source,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_match",
            entity_id=match.id,
            action="P2P_MATCH_SETTLED",
            before_state=before_match,
            after_state=self.match_state(match),
            reason=verification_source,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="withdrawal_request",
            entity_id=context.withdrawal.id,
            action="P2P_WITHDRAWAL_COMPLETED",
            before_state=before_withdrawal,
            after_state=self.withdrawal_state(context.withdrawal),
            reason=verification_source,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="order",
            entity_id=context.order.id,
            action="P2P_ORDER_PAID_DELIVERY_PENDING",
            before_state=before_order,
            after_state=OrderService._order_state(context.order),
            reason=verification_source,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_outbox_event",
            entity_id=outbox.id,
            action="P2P_DELIVERY_OUTBOX_ENQUEUED",
            before_state=None,
            after_state=self.outbox_state(outbox),
        )
        self._enqueue_notification(
            aggregate_type="p2p_match",
            aggregate_id=match.id,
            notification_type="P2P_SETTLEMENT_COMPLETED",
            recipient_user_ids=[match.buyer_user_id, match.receiver_user_id],
            payload={
                "match_id": match.id,
                "order_id": context.order.id,
                "settlement_id": settlement.id,
                "amount_paise": settlement.amount_paise,
            },
            deduplication_key=f"p2p-settlement:{settlement.id}:notification:completed",
            actor_user_id=actor_user_id,
        )
        await self.session.flush()
        response = self.settlement_snapshot(match, context.order, settlement)
        record.response_payload = response
        await self._finish(commit=commit)
        return P2PMatchMutationResult(match, response, False)

    async def retry_delivery(
        self,
        *,
        order_id: UUID,
        actor_user_id: UUID,
        idempotency_key: str,
        commit: bool,
    ) -> dict[str, object]:
        """Retry only ticket allocation for an already-paid P2P order; it cannot debit again."""

        await self._require_admin(actor_user_id)
        order = await self._locked_order(order_id)
        scope = f"admin:{actor_user_id}:p2p.delivery-retry"
        request_fingerprint = fingerprint({"order_id": order_id, "action": "retry_delivery"})
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return self._replay_delivery(replay)
        if order.status == OrderStatus.FULFILLED and order.delivery_status == DeliveryStatus.DELIVERED:
            response = OrderService.snapshot(order)
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="p2p_delivery_retry",
                resource_id=order.id,
            )
            record.response_payload = response
            await self._finish(commit=commit)
            return response
        if order.status != OrderStatus.PAID or order.settlement_reference_id is None:
            raise ConflictError("ORDER_NOT_SETTLED", "Only a paid P2P order can be delivered")
        try:
            # A savepoint isolates allocation writes.  The outer unit of work
            # remains usable to record a durable FAILED delivery outcome and
            # its idempotency response without committing a caller's
            # ``commit=False`` transaction.
            async with self.session.begin_nested():
                result = await TicketAllocationService(self.session).allocate_paid_order(
                    order_id=order.id,
                    settlement_reference_id=order.settlement_reference_id,
                    idempotency_key=idempotency_key,
                    actor_user_id=actor_user_id,
                    commit=False,
                )
        except Exception as error:
            # The settlement remains PAID.  Record the independently retryable
            # delivery outcome after the allocation savepoint has rolled back.
            failed_order = await self._locked_order(order_id)
            if failed_order.status != OrderStatus.PAID:
                raise
            before = OrderService._order_state(failed_order)
            if failed_order.delivery_status != DeliveryStatus.FAILED:
                failed_order.delivery_status = DeliveryStatus.FAILED
                self.audit.record(
                    actor_user_id=actor_user_id,
                    entity_type="order",
                    entity_id=failed_order.id,
                    action="P2P_ORDER_DELIVERY_FAILED",
                    before_state=before,
                    after_state=OrderService._order_state(failed_order),
                    reason="Allocation failed after a durable payment settlement",
                )
            if isinstance(error, AppError):
                code = error.code
                detail = error.message
            else:
                code = "DELIVERY_FAILED"
                detail = "Delivery could not be completed; retry with a new idempotency key"
            response = canonical_payload(
                {
                    **OrderService.snapshot(failed_order),
                    "delivery_failure": {"code": code, "detail": detail},
                }
            )
            record = self.idempotency.record(
                actor_scope=scope,
                key=idempotency_key,
                request_fingerprint=request_fingerprint,
                resource_type="p2p_delivery_retry",
                resource_id=failed_order.id,
                status_code=409,
            )
            record.response_payload = response
            await self._finish(commit=commit)
            raise ConflictError(code, detail) from error
        response = result.response_payload
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_delivery_retry",
            resource_id=order.id,
        )
        record.response_payload = response
        await self._finish(commit=commit)
        return response

    async def deliver_paid_order_from_outbox(
        self,
        *,
        order_id: UUID,
        outbox_attempt_key: str,
        commit: bool,
    ) -> dict[str, object]:
        """System-only delivery worker path for an already-paid P2P order.

        It shares the same savepoint/idempotency protections as the admin retry
        endpoint, while deliberately requiring no user/admin identity for a
        post-commit durable outbox consumer.
        """

        order = await self._locked_order(order_id)
        scope = "system:p2p.outbox-delivery"
        request_fingerprint = fingerprint(
            {"order_id": order_id, "outbox_attempt_key": outbox_attempt_key}
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=outbox_attempt_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return self._replay_delivery(replay)
        if order.status == OrderStatus.FULFILLED and order.delivery_status == DeliveryStatus.DELIVERED:
            response = OrderService.snapshot(order)
            record = self.idempotency.record(
                actor_scope=scope,
                key=outbox_attempt_key,
                request_fingerprint=request_fingerprint,
                resource_type="p2p_outbox_delivery",
                resource_id=order.id,
            )
            record.response_payload = response
            await self._finish(commit=commit)
            return response
        if order.status != OrderStatus.PAID or order.settlement_reference_id is None:
            raise ConflictError("ORDER_NOT_SETTLED", "Only a paid P2P order can be delivered")
        try:
            async with self.session.begin_nested():
                allocation = await TicketAllocationService(self.session).allocate_paid_order(
                    order_id=order.id,
                    settlement_reference_id=order.settlement_reference_id,
                    idempotency_key=outbox_attempt_key,
                    actor_user_id=None,
                    commit=False,
                )
        except Exception as error:
            failed_order = await self._locked_order(order_id)
            if failed_order.status != OrderStatus.PAID:
                raise
            before = OrderService._order_state(failed_order)
            if failed_order.delivery_status != DeliveryStatus.FAILED:
                failed_order.delivery_status = DeliveryStatus.FAILED
                self.audit.record(
                    actor_user_id=None,
                    entity_type="order",
                    entity_id=failed_order.id,
                    action="P2P_ORDER_OUTBOX_DELIVERY_FAILED",
                    before_state=before,
                    after_state=OrderService._order_state(failed_order),
                    reason="Durable delivery outbox allocation attempt failed",
                )
            if isinstance(error, AppError):
                code = error.code
                detail = error.message
            else:
                code = "DELIVERY_FAILED"
                detail = "Durable P2P delivery allocation failed"
            response = canonical_payload(
                {
                    **OrderService.snapshot(failed_order),
                    "delivery_failure": {"code": code, "detail": detail},
                }
            )
            record = self.idempotency.record(
                actor_scope=scope,
                key=outbox_attempt_key,
                request_fingerprint=request_fingerprint,
                resource_type="p2p_outbox_delivery",
                resource_id=failed_order.id,
                status_code=409,
            )
            record.response_payload = response
            await self._finish(commit=commit)
            raise ConflictError(code, detail) from error
        response = allocation.response_payload
        record = self.idempotency.record(
            actor_scope=scope,
            key=outbox_attempt_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_outbox_delivery",
            resource_id=order.id,
        )
        record.response_payload = response
        await self._finish(commit=commit)
        return response

    async def close_unpaid(
        self,
        *,
        match_id: UUID,
        actor_user_id: UUID,
        reason: str,
        evidence_reference: str,
        release_hold: bool,
        idempotency_key: str,
        commit: bool,
    ) -> P2PMatchMutationResult:
        """Close only after documented reconciliation; never close/release on a timer.

        A supported unpaid decision may either release the original hold and
        cancel the order, or retain the exact hold and return both sides to a
        safe, explicitly rematchable waiting state. Both branches preserve the
        old exposed attempt as ``CLOSED_UNPAID``.
        """

        await self._require_admin(actor_user_id)
        reason = self._reference_text(reason, field="reason", limit=500)
        evidence_reference = self._reference_text(evidence_reference, field="evidence_reference", limit=255)
        context = await self._locked_match_context(match_id)
        match = context.match
        scope = f"admin:{actor_user_id}:p2p.close-unpaid"
        request_fingerprint = fingerprint(
            {
                "match_id": match_id,
                "reason": reason,
                "evidence_reference": evidence_reference,
                "release_hold": release_hold,
            }
        )
        replay = await self.idempotency.get_replay(
            actor_scope=scope, key=idempotency_key, request_fingerprint=request_fingerprint
        )
        if replay is not None:
            return await self._replay_match(replay)
        if match.status not in {
            P2PMatchStatus.WAITING_FOR_PAYMENT,
            P2PMatchStatus.PAYMENT_SUBMITTED,
            P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION,
            P2PMatchStatus.UNDER_VERIFICATION,
            P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION,
            P2PMatchStatus.DISPUTED,
            P2PMatchStatus.ADMIN_REVIEW,
        }:
            raise ConflictError("INVALID_TRANSITION", "Only an unresolved P2P attempt can be closed unpaid")
        if context.order.status != OrderStatus.AWAITING_PAYMENT:
            raise ConflictError("ORDER_NOT_AWAITING_PAYMENT", "Order is not tied to an unresolved P2P attempt")
        before_match = self.match_state(match)
        before_withdrawal = self.withdrawal_state(context.withdrawal)
        before_order = OrderService._order_state(context.order)
        resolution = P2PAdminResolution(
            id=uuid4(),
            match_id=match.id,
            dispute_id=None,
            actor_user_id=actor_user_id,
            decision=AdminResolutionDecision.CLOSE_UNPAID,
            reason=reason,
            evidence_reference=evidence_reference,
            verification_source="MANUAL_RECONCILIATION",
        )
        self.session.add(resolution)
        self._transition_match(match, P2PMatchStatus.CLOSED_UNPAID)
        now = datetime.now(timezone.utc)
        match.closed_at = now
        match.instructions_disabled_at = now
        if release_hold:
            await self.wallets.release_locked(
                wallet_id=context.wallet.id,
                actor_user_id=actor_user_id,
                amount_paise=context.withdrawal.amount_paise,
                idempotency_key=idempotency_key,
                reference_type=P2P_WITHDRAWAL_REFERENCE_TYPE,
                reference_id=context.withdrawal.id,
                reason="Admin-evidenced P2P unpaid closure releases the original hold",
                commit=False,
            )
            self._transition_withdrawal(context.withdrawal, WithdrawalStatus.CANCELLED)
            context.withdrawal.cancelled_at = now
            await OrderService(self.session)._cancel_locked(
                order=context.order,
                actor_user_id=actor_user_id,
                scope=f"admin:{actor_user_id}:p2p.close-unpaid-order",
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint(
                    {"order_id": context.order.id, "match_id": match.id, "action": "p2p_close_unpaid"}
                ),
                action="P2P_ORDER_CANCELLED_AFTER_UNPAID_RECONCILIATION",
                reason=reason,
                commit=False,
                allow_reconciled_p2p=True,
            )
        else:
            self._transition_withdrawal(context.withdrawal, WithdrawalStatus.WAITING_FOR_BUYER)
            require_transition(
                current=context.order.status,
                target=OrderStatus.WAITING_FOR_MATCH,
                transitions=ORDER_TRANSITIONS,
                resource="Order",
            )
            context.order.status = OrderStatus.WAITING_FOR_MATCH
            context.order.expires_at = now + timedelta(minutes=get_settings().order_reservation_minutes)
            for reservation in context.order.reservations:
                if reservation.status.value == "RESERVED":
                    reservation.expires_at = context.order.expires_at
        dispute = await get_dispute_for_match(self.session, match.id, for_update=True)
        if dispute is not None and dispute.status != P2PDisputeStatus.RESOLVED:
            before_dispute = self.dispute_state(dispute)
            self._transition_dispute(dispute, P2PDisputeStatus.RESOLVED)
            dispute.resolved_at = now
        else:
            before_dispute = None
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_match",
            resource_id=match.id,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_admin_resolution",
            entity_id=resolution.id,
            action="P2P_ADMIN_UNPAID_CLOSURE_RECORDED",
            before_state=None,
            after_state=self.admin_resolution_state(resolution),
            reason=reason,
        )
        if dispute is not None and before_dispute is not None:
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="p2p_dispute",
                entity_id=dispute.id,
                action="P2P_DISPUTE_RESOLVED_BY_UNPAID_CLOSURE",
                before_state=before_dispute,
                after_state=self.dispute_state(dispute),
                reason=reason,
            )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_match",
            entity_id=match.id,
            action="P2P_MATCH_CLOSED_UNPAID",
            before_state=before_match,
            after_state=self.match_state(match),
            reason=reason,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="withdrawal_request",
            entity_id=context.withdrawal.id,
            action=("P2P_WITHDRAWAL_CANCELLED_AFTER_UNPAID_CLOSURE" if release_hold else "P2P_WITHDRAWAL_REMATCHABLE"),
            before_state=before_withdrawal,
            after_state=self.withdrawal_state(context.withdrawal),
            reason=reason,
        )
        if not release_hold:
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="order",
                entity_id=context.order.id,
                action="P2P_ORDER_RETURNED_TO_MATCHING_QUEUE",
                before_state=before_order,
                after_state=OrderService._order_state(context.order),
                reason=reason,
            )
        await self.session.flush()
        response = self.queue_snapshot(context.order, None)
        response["closed_match"] = self.match_snapshot(match, include_destination=False)
        record.response_payload = canonical_payload(response)
        await self._finish(commit=commit)
        return P2PMatchMutationResult(match, record.response_payload, False)

    async def _locked_match_context(self, match_id: UUID) -> _MatchContext:
        initial = await get_match(self.session, match_id)
        if initial is None:
            raise ValidationError("UNKNOWN_MATCH", "P2P match does not exist")
        order = await self._locked_order(initial.order_id)
        withdrawal = await get_withdrawal(self.session, initial.withdrawal_id, for_update=True)
        if withdrawal is None:
            raise ConflictError("MATCH_INCONSISTENT", "Match withdrawal does not exist")
        wallet = await get_wallet_by_id(self.session, withdrawal.wallet_id, for_update=True)
        if wallet is None:
            raise ConflictError("MATCH_INCONSISTENT", "Match withdrawal wallet does not exist")
        match = await get_match(self.session, match_id, for_update=True)
        if match is None:
            raise ValidationError("UNKNOWN_MATCH", "P2P match does not exist")
        if match.order_id != order.id or match.withdrawal_id != withdrawal.id:
            raise ConflictError("MATCH_INCONSISTENT", "Match links changed unexpectedly")
        return _MatchContext(order=order, withdrawal=withdrawal, wallet=wallet, match=match)

    async def _locked_order(self, order_id: UUID) -> Order:
        order = await get_order(self.session, order_id, for_update=True)
        if order is None:
            raise ValidationError("UNKNOWN_ORDER", "Order does not exist")
        return order

    async def _active_user(self, user_id: UUID) -> User:
        user = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == user_id)
        )
        if user is None or not user.is_active:
            raise AuthorizationError("An active user account is required")
        return user

    async def _require_admin(self, user_id: UUID) -> User:
        user = await self._active_user(user_id)
        if RoleName.ADMIN.value not in {role.name for role in user.roles}:
            raise AuthorizationError("This P2P resolution requires an administrator")
        return user

    @staticmethod
    def _assert_buyer(order: Order, buyer_user_id: UUID) -> None:
        if order.buyer_user_id != buyer_user_id:
            raise AuthorizationError("Only the buyer can queue this order for P2P matching")

    @staticmethod
    def _candidate_is_matchable(
        withdrawal: WithdrawalRequest,
        destination: PaymentDestination | None,
        wallet: Wallet | None,
        hold: WalletHold | None,
        order: Order,
    ) -> bool:
        if withdrawal.status != WithdrawalStatus.WAITING_FOR_BUYER:
            return False
        if withdrawal.amount_paise != order.total_paise or withdrawal.currency != order.currency:
            return False
        if withdrawal.wallet_hold_id is None or hold is None or hold.id != withdrawal.wallet_hold_id:
            return False
        if hold.status != WalletHoldStatus.ACTIVE or hold.amount_paise != withdrawal.amount_paise:
            return False
        if wallet is None or wallet.id != withdrawal.wallet_id or wallet.locked_paise < withdrawal.amount_paise:
            return False
        if wallet.currency != order.currency:
            return False
        if destination is None or destination.user_id != withdrawal.user_id:
            return False
        if (
            destination.status != PaymentDestinationStatus.VERIFIED
            or not destination.destination_data
            or not destination.verification_method
            or not destination.verification_evidence_reference
            or destination.verified_at is None
        ):
            return False
        if (
            not isinstance(withdrawal.rule_snapshot, dict)
            or not isinstance(withdrawal.rule_version, str)
            or not withdrawal.rule_version.strip()
        ):
            return False
        snapshot = withdrawal.rule_snapshot
        required_integer_fields = (
            "percentage_bps",
            "configured_min_withdrawal_paise",
            "configured_max_withdrawal_paise",
            "eligible_available_before_hold_paise",
        )
        if any(type(snapshot.get(field)) is not int for field in required_integer_fields):
            return False
        if type(snapshot.get("threshold_override_enabled")) is not bool:
            return False
        threshold = snapshot.get("threshold_paise")
        if type(threshold) is not int:
            return False
        percentage = snapshot["percentage_bps"]
        configured_minimum = snapshot["configured_min_withdrawal_paise"]
        configured_maximum = snapshot["configured_max_withdrawal_paise"]
        eligible_before_hold = snapshot["eligible_available_before_hold_paise"]
        if not (
            0 <= percentage <= 10_000
            and configured_minimum > 0
            and configured_maximum >= configured_minimum
            and eligible_before_hold >= 0
            and threshold >= 0
            and withdrawal.eligible_balance_snapshot_paise == eligible_before_hold
        ):
            return False
        maximum = (eligible_before_hold * percentage) // 10_000
        if snapshot["threshold_override_enabled"] and eligible_before_hold <= threshold:
            maximum = eligible_before_hold
        maximum = min(maximum, configured_maximum)
        return (
            withdrawal.max_amount_snapshot_paise == maximum
            and configured_minimum <= withdrawal.amount_paise <= maximum
        )

    @staticmethod
    def _frozen_destination_snapshot(destination: PaymentDestination) -> dict[str, object]:
        return canonical_payload(
            {
                "destination_id": destination.id,
                "provider_namespace": destination.provider_namespace,
                "display_label": destination.display_label,
                "recipient": destination.destination_data,
            }
        )

    @staticmethod
    def _reference_text(value: object, *, field: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise ValidationError("INVALID_REFERENCE", f"{field} must be a non-empty string up to {limit} characters")
        return value.strip()

    @staticmethod
    def _optional_text(value: object, *, field: str, limit: int) -> str | None:
        if value is None:
            return None
        return P2PService._reference_text(value, field=field, limit=limit)

    @staticmethod
    def _digest_text(value: object, *, field: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in value)
        ):
            raise ValidationError("INVALID_REFERENCE", f"{field} must be a SHA-256 hexadecimal digest")
        return value.lower()

    @staticmethod
    def _recipient_fingerprint(match: P2PMatch) -> str:
        recipient = match.destination_snapshot.get("recipient")
        if not isinstance(recipient, dict):
            raise ConflictError("MATCH_INCONSISTENT", "Frozen P2P recipient data is invalid")
        return canonical_payload_digest(recipient)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise ValidationError("INVALID_TIMESTAMP", "Timestamp must be a datetime")
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _transition_match(match: P2PMatch, target: P2PMatchStatus) -> None:
        require_transition(
            current=match.status, target=target, transitions=P2P_MATCH_TRANSITIONS, resource="P2P match"
        )
        match.status = target

    @staticmethod
    def _transition_withdrawal(withdrawal: WithdrawalRequest, target: WithdrawalStatus) -> None:
        require_transition(
            current=withdrawal.status,
            target=target,
            transitions=WITHDRAWAL_TRANSITIONS,
            resource="Withdrawal",
        )
        withdrawal.status = target

    @staticmethod
    def _transition_dispute(dispute: P2PDispute, target: P2PDisputeStatus) -> None:
        require_transition(
            current=dispute.status,
            target=target,
            transitions=P2P_DISPUTE_TRANSITIONS,
            resource="P2P dispute",
        )
        dispute.status = target

    async def _lock_payment_reference(self, provider_namespace: str, reference: str) -> None:
        """Lock a logical reference before querying rows that may not exist yet."""

        await lock_key_for_transaction(self.session, "p2p-payment-reference", provider_namespace, reference)

    async def _record_reference_conflict(
        self,
        *,
        context: _MatchContext,
        submission: P2PPaymentSubmission,
        actor_user_id: UUID | None,
        scope: str,
        idempotency_key: str,
        request_fingerprint: str,
        commit: bool,
    ) -> P2PMatchMutationResult:
        """Persist a safe review outcome for attempted cross-match reference reuse.

        This deliberately returns a durable review response rather than raising
        after mutation.  Raising would cause FastAPI's request session to close
        without a commit and lose the evidence/review transition that protects
        the original match.
        """

        match = context.match
        before_submission = self.submission_state(submission)
        before_match = self.match_state(match)
        before_withdrawal = self.withdrawal_state(context.withdrawal)
        # A reference conflict flags the attempt for review; it must never
        # overwrite verification that already completed.  The match still moves
        # to admin review so a human resolves the ambiguity.
        if submission.verification_status != PaymentSubmissionVerificationStatus.VERIFIED:
            submission.verification_status = PaymentSubmissionVerificationStatus.MANUAL_REVIEW
            submission.verification_reason = "REFERENCE_CONFLICT"
            submission.verified_at = None
        self._move_match_to_admin_review(match)
        withdrawal_changed = False
        if context.withdrawal.status == WithdrawalStatus.MATCHED:
            self._transition_withdrawal(context.withdrawal, WithdrawalStatus.UNDER_REVIEW)
            withdrawal_changed = True
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_payment_submission",
            resource_id=submission.id,
            status_code=409,
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_payment_submission",
            entity_id=submission.id,
            action="P2P_PAYMENT_SUBMISSION_REFERENCE_CONFLICT",
            before_state=before_submission,
            after_state=self.submission_state(submission),
            reason="Reference is already verified for a different payment attempt",
        )
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_match",
            entity_id=match.id,
            action="P2P_MATCH_REFERENCE_CONFLICT_REVIEW_REQUIRED",
            before_state=before_match,
            after_state=self.match_state(match),
            reason="Cross-match verified payment reference reuse",
        )
        if withdrawal_changed:
            self.audit.record(
                actor_user_id=actor_user_id,
                entity_type="withdrawal_request",
                entity_id=context.withdrawal.id,
                action="P2P_WITHDRAWAL_REFERENCE_CONFLICT_REVIEW_REQUIRED",
                before_state=before_withdrawal,
                after_state=self.withdrawal_state(context.withdrawal),
                reason="Cross-match verified payment reference reuse",
            )
        self._enqueue_notification(
            aggregate_type="p2p_match",
            aggregate_id=match.id,
            notification_type="P2P_REVIEW_REQUIRED",
            recipient_user_ids=[match.buyer_user_id, match.receiver_user_id],
            payload={
                "match_id": match.id,
                "submission_id": submission.id,
                "reason_code": "REFERENCE_CONFLICT",
            },
            deduplication_key=f"p2p-submission:{submission.id}:notification:reference-conflict",
            actor_user_id=actor_user_id,
        )
        await self.session.flush()
        response = canonical_payload(
            {
                **self.submission_snapshot(match, submission),
                "review_required": True,
                "code": "REFERENCE_CONFLICT",
            }
        )
        record.response_payload = response
        await self._finish(commit=commit)
        return P2PMatchMutationResult(match, response, False)

    async def _record_provider_review(
        self,
        *,
        context: _MatchContext,
        submission: P2PPaymentSubmission,
        scope: str,
        idempotency_key: str,
        request_fingerprint: str,
        reason_code: str,
        reason: str,
        commit: bool,
    ) -> P2PMatchMutationResult:
        """Persist a non-settling review outcome for uncertain provider evidence."""

        match = context.match
        before_submission = self.submission_state(submission)
        before_match = self.match_state(match)
        before_withdrawal = self.withdrawal_state(context.withdrawal)
        # A late or uncertain provider event is evidence for review, not
        # permission to rewrite history.  Preserve an already-VERIFIED
        # submission (including a settled one) instead of demoting it.
        if submission.verification_status != PaymentSubmissionVerificationStatus.VERIFIED:
            submission.verification_status = PaymentSubmissionVerificationStatus.MANUAL_REVIEW
            submission.verification_reason = reason_code
            submission.verified_at = None
        match_changed = False
        if match.status in {
            P2PMatchStatus.WAITING_FOR_PAYMENT,
            P2PMatchStatus.PAYMENT_SUBMITTED,
            P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION,
            P2PMatchStatus.UNDER_VERIFICATION,
            P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION,
            P2PMatchStatus.DISPUTED,
        }:
            self._move_match_to_admin_review(match)
            match_changed = True
        withdrawal_changed = False
        if context.withdrawal.status == WithdrawalStatus.MATCHED:
            self._transition_withdrawal(context.withdrawal, WithdrawalStatus.UNDER_REVIEW)
            withdrawal_changed = True
        record = self.idempotency.record(
            actor_scope=scope,
            key=idempotency_key,
            request_fingerprint=request_fingerprint,
            resource_type="p2p_payment_submission",
            resource_id=submission.id,
            status_code=409,
        )
        self.audit.record(
            actor_user_id=None,
            entity_type="p2p_payment_submission",
            entity_id=submission.id,
            action="P2P_PROVIDER_EVIDENCE_REVIEW_REQUIRED",
            before_state=before_submission,
            after_state=self.submission_state(submission),
            reason=reason,
        )
        if match_changed:
            self.audit.record(
                actor_user_id=None,
                entity_type="p2p_match",
                entity_id=match.id,
                action="P2P_MATCH_PROVIDER_EVIDENCE_REVIEW_REQUIRED",
                before_state=before_match,
                after_state=self.match_state(match),
                reason=reason,
            )
        if withdrawal_changed:
            self.audit.record(
                actor_user_id=None,
                entity_type="withdrawal_request",
                entity_id=context.withdrawal.id,
                action="P2P_WITHDRAWAL_PROVIDER_EVIDENCE_REVIEW_REQUIRED",
                before_state=before_withdrawal,
                after_state=self.withdrawal_state(context.withdrawal),
                reason=reason,
            )
        self._enqueue_notification(
            aggregate_type="p2p_match",
            aggregate_id=match.id,
            notification_type="P2P_REVIEW_REQUIRED",
            recipient_user_ids=[match.buyer_user_id, match.receiver_user_id],
            payload={
                "match_id": match.id,
                "submission_id": submission.id,
                "reason_code": reason_code,
            },
            deduplication_key=f"p2p-submission:{submission.id}:notification:provider-review:{idempotency_key}",
            actor_user_id=None,
        )
        await self.session.flush()
        response = canonical_payload(
            {
                **self.submission_snapshot(match, submission),
                "review_required": True,
                "code": "REVIEW_REQUIRED",
            }
        )
        record.response_payload = response
        await self._finish(commit=commit)
        return P2PMatchMutationResult(match, response, False)

    def _enqueue_notification(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        notification_type: str,
        recipient_user_ids: list[UUID],
        payload: dict[str, object],
        deduplication_key: str,
        actor_user_id: UUID | None,
    ) -> P2POutboxEvent:
        """Add an in-app notification request to the same domain transaction."""

        recipients = sorted({str(user_id) for user_id in recipient_user_ids})
        if not recipients:
            raise ValidationError("INVALID_NOTIFICATION", "A P2P notification requires at least one recipient")
        event = P2POutboxEvent(
            id=uuid4(),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type="P2P_NOTIFICATION_REQUESTED",
            deduplication_key=deduplication_key,
            payload=canonical_payload(
                {
                    "notification_type": notification_type,
                    "recipient_user_ids": recipients,
                    **payload,
                }
            ),
            status=OutboxEventStatus.PENDING,
        )
        self.session.add(event)
        self.audit.record(
            actor_user_id=actor_user_id,
            entity_type="p2p_outbox_event",
            entity_id=event.id,
            action="P2P_NOTIFICATION_OUTBOX_ENQUEUED",
            before_state=None,
            after_state=self.outbox_state(event),
            reason=notification_type,
        )
        return event

    def _move_match_to_admin_review(self, match: P2PMatch) -> None:
        if match.status == P2PMatchStatus.ADMIN_REVIEW:
            return
        if match.status == P2PMatchStatus.WAITING_FOR_PAYMENT:
            self._transition_match(match, P2PMatchStatus.ADMIN_REVIEW)
        elif match.status == P2PMatchStatus.DISPUTED:
            self._transition_match(match, P2PMatchStatus.ADMIN_REVIEW)
        elif match.status == P2PMatchStatus.UNDER_VERIFICATION:
            self._transition_match(match, P2PMatchStatus.ADMIN_REVIEW)
        elif match.status == P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION:
            self._transition_match(match, P2PMatchStatus.ADMIN_REVIEW)
        elif match.status == P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION:
            self._transition_match(match, P2PMatchStatus.UNDER_VERIFICATION)
            self._transition_match(match, P2PMatchStatus.ADMIN_REVIEW)
        elif match.status == P2PMatchStatus.PAYMENT_SUBMITTED:
            self._transition_match(match, P2PMatchStatus.UNDER_VERIFICATION)
            self._transition_match(match, P2PMatchStatus.ADMIN_REVIEW)
        else:
            raise ConflictError("INVALID_TRANSITION", "Match cannot enter admin review from its current state")

    async def _replay_match(self, record: IdempotencyRecord) -> P2PMatchMutationResult:
        if record.resource_id is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior P2P mutation cannot be recovered")
        match_id_value = record.response_payload.get("match", {}).get("id") if isinstance(record.response_payload.get("match"), dict) else None
        match_id = UUID(str(match_id_value)) if match_id_value is not None else None
        match = await get_match(self.session, match_id) if match_id is not None else None
        return P2PMatchMutationResult(match, dict(record.response_payload), True)

    @staticmethod
    def _replay_delivery(record: IdempotencyRecord) -> dict[str, object]:
        if record.resource_id is None or not isinstance(record.response_payload, dict):
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior delivery retry cannot be recovered")
        failure = record.response_payload.get("delivery_failure")
        if isinstance(failure, dict):
            code = failure.get("code")
            detail = failure.get("detail")
            if isinstance(code, str) and isinstance(detail, str):
                raise ConflictError(code, detail)
            raise ConflictError("IDEMPOTENCY_INCOMPLETE", "Prior delivery failure is invalid")
        return dict(record.response_payload)

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()

    @classmethod
    def withdrawal_state(cls, withdrawal: WithdrawalRequest) -> dict[str, object]:
        return {
            "id": withdrawal.id,
            "user_id": withdrawal.user_id,
            "wallet_id": withdrawal.wallet_id,
            "amount_paise": withdrawal.amount_paise,
            "currency": withdrawal.currency,
            "status": withdrawal.status,
            "wallet_hold_id": withdrawal.wallet_hold_id,
        }

    @classmethod
    def match_state(cls, match: P2PMatch) -> dict[str, object]:
        """Audit-safe match state: never copy recipient data into an audit payload."""

        return {
            "id": match.id,
            "order_id": match.order_id,
            "withdrawal_id": match.withdrawal_id,
            "buyer_user_id": match.buyer_user_id,
            "receiver_user_id": match.receiver_user_id,
            "amount_paise": match.amount_paise,
            "currency": match.currency,
            "status": match.status,
            "destination_exposed": match.instructions_exposed_at is not None,
            "payment_deadline_at": cls._datetime(match.payment_deadline_at),
            "receiver_confirmation_deadline_at": cls._datetime(match.receiver_confirmation_deadline_at),
            "instructions_disabled_at": cls._datetime(match.instructions_disabled_at),
        }

    @classmethod
    def match_snapshot(cls, match: P2PMatch, *, include_destination: bool) -> dict[str, object]:
        result = cls.match_state(match)
        result["instructions_exposed_at"] = cls._datetime(match.instructions_exposed_at)
        result["settled_at"] = cls._datetime(match.settled_at)
        result["closed_at"] = cls._datetime(match.closed_at)
        result["destination_snapshot"] = match.destination_snapshot if include_destination else None
        return canonical_payload(result)

    @classmethod
    def submission_state(cls, submission: P2PPaymentSubmission) -> dict[str, object]:
        return {
            "id": submission.id,
            "match_id": submission.match_id,
            "buyer_user_id": submission.buyer_user_id,
            "provider_namespace": submission.provider_namespace,
            "claimed_reference": submission.claimed_reference,
            "expected_amount_paise": submission.expected_amount_paise,
            "expected_currency": submission.expected_currency,
            "observed_amount_paise": submission.observed_amount_paise,
            "observed_currency": submission.observed_currency,
            "declared_paid_at": cls._datetime(submission.declared_paid_at),
            "evidence_upload_id": submission.evidence_upload_id,
            "provider_evidence_reference": submission.provider_evidence_reference,
            "verification_status": submission.verification_status,
            "verification_reason": submission.verification_reason,
            "is_late": submission.is_late,
        }

    @classmethod
    def dispute_state(cls, dispute: P2PDispute) -> dict[str, object]:
        return {
            "id": dispute.id,
            "match_id": dispute.match_id,
            "opened_by_user_id": dispute.opened_by_user_id,
            "status": dispute.status,
            "reason_code": dispute.reason_code,
            "evidence_reference": dispute.evidence_reference,
            "resolved_at": cls._datetime(dispute.resolved_at),
        }

    @classmethod
    def confirmation_state(cls, confirmation: P2PReceiverConfirmation) -> dict[str, object]:
        return {
            "id": confirmation.id,
            "match_id": confirmation.match_id,
            "receiver_user_id": confirmation.receiver_user_id,
            "decision": confirmation.decision,
            "reason": confirmation.reason,
            "confirmed_at": cls._datetime(confirmation.confirmed_at),
        }

    @classmethod
    def admin_resolution_state(cls, resolution: P2PAdminResolution) -> dict[str, object]:
        return {
            "id": resolution.id,
            "match_id": resolution.match_id,
            "dispute_id": resolution.dispute_id,
            "actor_user_id": resolution.actor_user_id,
            "decision": resolution.decision,
            "verification_source": resolution.verification_source,
            "evidence_reference": resolution.evidence_reference,
        }

    @classmethod
    def outbox_state(cls, event: P2POutboxEvent) -> dict[str, object]:
        return {
            "id": event.id,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "event_type": event.event_type,
            "deduplication_key": event.deduplication_key,
            "status": event.status,
            "attempts": event.attempts,
        }

    @classmethod
    def queue_snapshot(cls, order: Order, match: P2PMatch | None) -> dict[str, object]:
        return canonical_payload(
            {
                "order": OrderService._order_state(order),
                "match": cls.match_snapshot(match, include_destination=True) if match is not None else None,
                "message": "Matched payment instructions are available" if match else "Waiting for an exact receiver",
            }
        )

    @classmethod
    def submission_snapshot(cls, match: P2PMatch, submission: P2PPaymentSubmission) -> dict[str, object]:
        return canonical_payload(
            {"match": cls.match_snapshot(match, include_destination=True), "submission": cls.submission_state(submission)}
        )

    @classmethod
    def confirmation_snapshot(cls, match: P2PMatch, confirmation: P2PReceiverConfirmation) -> dict[str, object]:
        return canonical_payload(
            {"match": cls.match_snapshot(match, include_destination=True), "confirmation": cls.confirmation_state(confirmation)}
        )

    @classmethod
    def settlement_snapshot(cls, match: P2PMatch, order: Order, settlement: P2PSettlement) -> dict[str, object]:
        return canonical_payload(
            {
                "match": cls.match_snapshot(match, include_destination=True),
                "order": OrderService._order_state(order),
                "settlement": {
                    "id": settlement.id,
                    "journal_group_id": settlement.journal_group_id,
                    "amount_paise": settlement.amount_paise,
                    "currency": settlement.currency,
                    "verification_source": settlement.verification_source,
                    "settled_at": cls._datetime(settlement.settled_at),
                },
            }
        )

    @staticmethod
    def _datetime(value: datetime | None) -> str | None:
        return P2PService._as_utc(value).isoformat() if value is not None else None
