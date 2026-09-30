"""P2P payment-attempt, evidence, resolution, settlement and outbox records."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class P2PMatchStatus(StrEnum):
    WAITING_FOR_PAYMENT = "WAITING_FOR_PAYMENT"
    PAYMENT_SUBMITTED = "PAYMENT_SUBMITTED"
    WAITING_FOR_RECEIVER_CONFIRMATION = "WAITING_FOR_RECEIVER_CONFIRMATION"
    UNDER_VERIFICATION = "UNDER_VERIFICATION"
    EXPIRED_AWAITING_RECONCILIATION = "EXPIRED_AWAITING_RECONCILIATION"
    DISPUTED = "DISPUTED"
    ADMIN_REVIEW = "ADMIN_REVIEW"
    SETTLED = "SETTLED"
    CLOSED_UNPAID = "CLOSED_UNPAID"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"


ACTIVE_MATCH_STATUSES = (
    P2PMatchStatus.WAITING_FOR_PAYMENT.value,
    P2PMatchStatus.PAYMENT_SUBMITTED.value,
    P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION.value,
    P2PMatchStatus.UNDER_VERIFICATION.value,
    P2PMatchStatus.EXPIRED_AWAITING_RECONCILIATION.value,
    P2PMatchStatus.DISPUTED.value,
    P2PMatchStatus.ADMIN_REVIEW.value,
    P2PMatchStatus.REFUND_PENDING.value,
)


class PaymentSubmissionVerificationStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class ReceiverConfirmationDecision(StrEnum):
    RECEIVED = "RECEIVED"
    NOT_RECEIVED = "NOT_RECEIVED"


class P2PDisputeStatus(StrEnum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED = "RESOLVED"


class AdminResolutionDecision(StrEnum):
    SETTLE = "SETTLE"
    CLOSE_UNPAID = "CLOSE_UNPAID"
    KEEP_IN_REVIEW = "KEEP_IN_REVIEW"


class P2PRefundStatus(StrEnum):
    PENDING_EVIDENCE = "PENDING_EVIDENCE"
    APPROVED = "APPROVED"
    PAID = "PAID"
    REJECTED = "REJECTED"


class OutboxEventStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class P2PProviderEventStatus(StrEnum):
    """Lifecycle for a signed, durably captured provider callback."""

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class P2PMatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable recipient-instruction exposure for one exact order/request pair."""

    __tablename__ = "p2p_matches"
    __table_args__ = (
        sa.CheckConstraint("amount_paise > 0", name="ck_p2p_match_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_p2p_match_currency_inr"),
        sa.Index(
            "uq_p2p_active_match_order",
            "order_id",
            unique=True,
            postgresql_where=sa.text(
                "status IN ('WAITING_FOR_PAYMENT', 'PAYMENT_SUBMITTED', "
                "'WAITING_FOR_RECEIVER_CONFIRMATION', 'UNDER_VERIFICATION', "
                "'EXPIRED_AWAITING_RECONCILIATION', 'DISPUTED', 'ADMIN_REVIEW', 'REFUND_PENDING')"
            ),
            sqlite_where=sa.text(
                "status IN ('WAITING_FOR_PAYMENT', 'PAYMENT_SUBMITTED', "
                "'WAITING_FOR_RECEIVER_CONFIRMATION', 'UNDER_VERIFICATION', "
                "'EXPIRED_AWAITING_RECONCILIATION', 'DISPUTED', 'ADMIN_REVIEW', 'REFUND_PENDING')"
            ),
        ),
        sa.Index(
            "uq_p2p_active_match_withdrawal",
            "withdrawal_id",
            unique=True,
            postgresql_where=sa.text(
                "status IN ('WAITING_FOR_PAYMENT', 'PAYMENT_SUBMITTED', "
                "'WAITING_FOR_RECEIVER_CONFIRMATION', 'UNDER_VERIFICATION', "
                "'EXPIRED_AWAITING_RECONCILIATION', 'DISPUTED', 'ADMIN_REVIEW', 'REFUND_PENDING')"
            ),
            sqlite_where=sa.text(
                "status IN ('WAITING_FOR_PAYMENT', 'PAYMENT_SUBMITTED', "
                "'WAITING_FOR_RECEIVER_CONFIRMATION', 'UNDER_VERIFICATION', "
                "'EXPIRED_AWAITING_RECONCILIATION', 'DISPUTED', 'ADMIN_REVIEW', 'REFUND_PENDING')"
            ),
        ),
    )

    order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    withdrawal_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("withdrawal_requests.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    buyer_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    receiver_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    destination_snapshot: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    status: Mapped[P2PMatchStatus] = mapped_column(
        sa.Enum(P2PMatchStatus, name="p2p_match_status"),
        nullable=False,
        default=P2PMatchStatus.WAITING_FOR_PAYMENT,
        server_default=P2PMatchStatus.WAITING_FOR_PAYMENT.value,
        index=True,
    )
    payment_deadline_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, index=True)
    receiver_confirmation_deadline_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True, index=True
    )
    instructions_exposed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    instructions_disabled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class P2PPaymentSubmission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A buyer claim/evidence record; it is explicitly not provider verification."""

    __tablename__ = "p2p_payment_submissions"
    __table_args__ = (
        sa.CheckConstraint("expected_amount_paise > 0", name="ck_p2p_submission_expected_positive"),
        sa.CheckConstraint("observed_amount_paise > 0", name="ck_p2p_submission_observed_positive"),
        sa.CheckConstraint("expected_currency = 'INR'", name="ck_p2p_submission_expected_inr"),
        sa.CheckConstraint("observed_currency = 'INR'", name="ck_p2p_submission_observed_inr"),
        sa.UniqueConstraint(
            "match_id", "provider_namespace", "claimed_reference", name="uq_p2p_submission_match_reference"
        ),
    )

    match_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_matches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    buyer_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    provider_namespace: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    claimed_reference: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    expected_amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    expected_currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    observed_amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    observed_currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    declared_paid_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    evidence_upload_id: Mapped[UUID | None] = mapped_column(sa.Uuid(as_uuid=True), nullable=True)
    provider_evidence_reference: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    evidence_metadata: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
    verification_status: Mapped[PaymentSubmissionVerificationStatus] = mapped_column(
        sa.Enum(PaymentSubmissionVerificationStatus, name="p2p_submission_verification_status"),
        nullable=False,
        default=PaymentSubmissionVerificationStatus.UNVERIFIED,
        server_default=PaymentSubmissionVerificationStatus.UNVERIFIED.value,
        index=True,
    )
    verification_reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    is_late: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default=sa.false())


class P2PVerifiedPaymentReference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Ownership is reserved only once a reference has been adjudicated as valid."""

    __tablename__ = "p2p_verified_payment_references"
    __table_args__ = (
        sa.UniqueConstraint("provider_namespace", "transaction_reference", name="uq_p2p_verified_reference"),
        sa.UniqueConstraint("payment_submission_id", name="uq_p2p_verified_reference_submission"),
    )

    payment_submission_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("p2p_payment_submissions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    match_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_matches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider_namespace: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    transaction_reference: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    verification_source: Mapped[str] = mapped_column(sa.String(100), nullable=False)


class P2PReceiverConfirmation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "p2p_receiver_confirmations"
    __table_args__ = (sa.UniqueConstraint("match_id", name="uq_p2p_receiver_confirmation_match"),)

    match_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_matches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    receiver_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[ReceiverConfirmationDecision] = mapped_column(
        sa.Enum(ReceiverConfirmationDecision, name="receiver_confirmation_decision"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class P2PDispute(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "p2p_disputes"
    __table_args__ = (sa.UniqueConstraint("match_id", name="uq_p2p_dispute_match"),)

    match_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_matches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    opened_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[P2PDisputeStatus] = mapped_column(
        sa.Enum(P2PDisputeStatus, name="p2p_dispute_status"),
        nullable=False,
        default=P2PDisputeStatus.OPEN,
        server_default=P2PDisputeStatus.OPEN.value,
        index=True,
    )
    reason_code: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    evidence_reference: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class P2PAdminResolution(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "p2p_admin_resolutions"

    match_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_matches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dispute_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_disputes.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[AdminResolutionDecision] = mapped_column(
        sa.Enum(AdminResolutionDecision, name="admin_resolution_decision"), nullable=False
    )
    reason: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    evidence_reference: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    verification_source: Mapped[str] = mapped_column(sa.String(100), nullable=False)


class P2PSettlement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The unique settlement bridge from P2P evidence to an order entitlement."""

    __tablename__ = "p2p_settlements"
    __table_args__ = (
        sa.UniqueConstraint("match_id", name="uq_p2p_settlement_match"),
        sa.UniqueConstraint("withdrawal_id", name="uq_p2p_settlement_withdrawal"),
        sa.UniqueConstraint("order_id", name="uq_p2p_settlement_order"),
        sa.UniqueConstraint("journal_group_id", name="uq_p2p_settlement_journal"),
        sa.CheckConstraint("amount_paise > 0", name="ck_p2p_settlement_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_p2p_settlement_currency_inr"),
    )

    match_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_matches.id", ondelete="RESTRICT"), nullable=False
    )
    withdrawal_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("withdrawal_requests.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    journal_group_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("journal_groups.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_record_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("idempotency_records.id", ondelete="RESTRICT"), nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    verification_source: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    settled_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    settled_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class P2PRefund(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A separately evidenced post-settlement refund obligation, never a wallet release.

    Phase 4 can create only ``PENDING_EVIDENCE`` cases.  A future funded
    payout workflow must supply the verified payout fields before the record
    can become ``PAID``; it must never reuse the receiver's settled hold.
    """

    __tablename__ = "p2p_refunds"
    __table_args__ = (
        sa.CheckConstraint("amount_paise > 0", name="ck_p2p_refund_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_p2p_refund_currency_inr"),
        sa.CheckConstraint(
            "status <> 'PAID' OR (payout_reference IS NOT NULL "
            "AND payout_verified_by_user_id IS NOT NULL AND payout_verified_at IS NOT NULL)",
            name="ck_p2p_refund_paid_verification",
        ),
        sa.UniqueConstraint("match_id", name="uq_p2p_refund_match"),
    )

    match_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_matches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    settlement_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_settlements.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    verified_payment_reference_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("p2p_verified_payment_references.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    liable_party: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    funding_source_reference: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    destination_validation_reference: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    executor_reference: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    payout_reference: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    payout_verified_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    payout_verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    status: Mapped[P2PRefundStatus] = mapped_column(
        sa.Enum(P2PRefundStatus, name="p2p_refund_status"),
        nullable=False,
        default=P2PRefundStatus.PENDING_EVIDENCE,
        server_default=P2PRefundStatus.PENDING_EVIDENCE.value,
        index=True,
    )


class P2POutboxEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable, retryable work emitted only after a P2P transition commits."""

    __tablename__ = "p2p_outbox_events"
    __table_args__ = (sa.UniqueConstraint("deduplication_key", name="uq_p2p_outbox_deduplication"),)

    aggregate_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False, index=True)
    deduplication_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    status: Mapped[OutboxEventStatus] = mapped_column(
        sa.Enum(OutboxEventStatus, name="p2p_outbox_event_status"),
        nullable=False,
        default=OutboxEventStatus.PENDING,
        server_default=OutboxEventStatus.PENDING.value,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class P2PProviderEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A signed provider callback retained before it can affect settlement."""

    __tablename__ = "p2p_provider_events"
    __table_args__ = (
        sa.UniqueConstraint(
            "provider_namespace", "external_event_id", name="uq_p2p_provider_event_external"
        ),
        sa.CheckConstraint(
            "verified_amount_paise IS NULL OR verified_amount_paise > 0",
            name="ck_p2p_provider_event_amount_positive",
        ),
        sa.CheckConstraint(
            "verified_currency IS NULL OR verified_currency = 'INR'",
            name="ck_p2p_provider_event_currency_inr",
        ),
    )

    provider_namespace: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    external_event_id: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    match_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_matches.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    payment_submission_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("p2p_payment_submissions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    transaction_reference: Mapped[str | None] = mapped_column(sa.String(160), nullable=True, index=True)
    verified_amount_paise: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    verified_currency: Mapped[str | None] = mapped_column(sa.String(3), nullable=True)
    recipient_fingerprint: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    status: Mapped[P2PProviderEventStatus] = mapped_column(
        sa.Enum(P2PProviderEventStatus, name="p2p_provider_event_status"),
        nullable=False,
        default=P2PProviderEventStatus.RECEIVED,
        server_default=P2PProviderEventStatus.RECEIVED.value,
        index=True,
    )
    processing_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class P2PNotification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An in-app, durable notification emitted by the P2P outbox dispatcher."""

    __tablename__ = "p2p_notifications"
    __table_args__ = (
        sa.UniqueConstraint("outbox_event_id", "user_id", name="uq_p2p_notification_outbox_user"),
    )

    outbox_event_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("p2p_outbox_events.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False, index=True)
    payload: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
