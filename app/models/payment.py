"""Provider-agnostic payment persistence with explicit settlement evidence.

P2P payment records remain in :mod:`app.models.p2p_match` because their
receiver-confirmation rules are intentionally distinct. This module models
all other order payment methods and keeps provider values separate from the
order's immutable INR-paise value. In particular, Telegram Stars use ``XTR``
and must never be coerced into INR paise.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.p2p_match import P2PPaymentSubmission, P2PSettlement, P2PVerifiedPaymentReference


class PaymentMethod(StrEnum):
    """The externally funded checkout mechanisms supported in Phase 5."""

    WHITE_LABEL = "WHITE_LABEL"
    TELEGRAM_STARS = "TELEGRAM_STARS"
    MANUAL_UPI = "MANUAL_UPI"


class PaymentAttemptStatus(StrEnum):
    """A payment attempt's independently auditable lifecycle."""

    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    UNDER_REVIEW = "UNDER_REVIEW"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PaymentProviderEventStatus(StrEnum):
    """Processing state for a durably retained provider callback."""

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class PaymentOutboxEventStatus(StrEnum):
    """Retry state for post-commit payment work."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class PaymentReconciliationStatus(StrEnum):
    """Lifecycle of a bounded provider reconciliation run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ManualPaymentDestinationStatus(StrEnum):
    """An admin-owned manual UPI destination is exposed only when approved."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ManualPaymentProofStatus(StrEnum):
    """A screenshot/UTR submission is evidence, not settlement by itself."""

    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


ACTIVE_PAYMENT_ATTEMPT_STATUSES = (
    PaymentAttemptStatus.AWAITING_PAYMENT.value,
    PaymentAttemptStatus.UNDER_REVIEW.value,
)
ACTIVE_PAYMENT_ATTEMPT_SQL = "status IN ('AWAITING_PAYMENT', 'UNDER_REVIEW')"


class ManualPaymentDestination(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Approved UPI/QR instructions owned by platform operations.

    The approval fields deliberately live with the destination rather than in
    mutable runtime configuration. A historical payment attempt keeps its own
    snapshot, so later edits or disabling cannot rewrite instructions that
    were already exposed to a buyer.
    """

    __tablename__ = "manual_payment_destinations"
    __table_args__ = (
        sa.CheckConstraint(
            "(status <> 'ACTIVE') OR (approved_by_user_id IS NOT NULL "
            "AND approval_evidence_reference IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_manual_payment_destination_active_approved",
        ),
        # Replacing the active destination is a single logical operation.  A
        # partial unique index keeps concurrent administrators from exposing
        # two active instruction sets during the empty-row race.
        sa.Index(
            "uq_manual_payment_destination_one_active",
            "status",
            unique=True,
            postgresql_where=sa.text("status = 'ACTIVE'"),
            sqlite_where=sa.text("status = 'ACTIVE'"),
        ),
    )

    display_label: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    upi_id: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    qr_reference: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    instructions: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    status: Mapped[ManualPaymentDestinationStatus] = mapped_column(
        sa.Enum(ManualPaymentDestinationStatus, name="manual_payment_destination_status"),
        nullable=False,
        default=ManualPaymentDestinationStatus.PENDING_APPROVAL,
        server_default=ManualPaymentDestinationStatus.PENDING_APPROVAL.value,
        index=True,
    )
    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approval_evidence_reference: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class PaymentAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Frozen checkout amount, instructions, and external-provider identifiers.

    ``order_amount_paise`` / ``order_currency`` are the immutable order value.
    ``provider_amount`` / ``provider_currency`` are intentionally separate
    integer provider units; for example, Stars are ``XTR`` rather than paise.
    """

    __tablename__ = "payment_attempts"
    __table_args__ = (
        sa.CheckConstraint("order_amount_paise > 0", name="ck_payment_attempt_order_amount_positive"),
        sa.CheckConstraint("order_currency = 'INR'", name="ck_payment_attempt_order_currency_inr"),
        sa.CheckConstraint("provider_amount > 0", name="ck_payment_attempt_provider_amount_positive"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_payment_attempt_idempotency"),
        sa.UniqueConstraint("merchant_reference", name="uq_payment_attempt_merchant_reference"),
        sa.UniqueConstraint(
            "provider_namespace",
            "provider_payment_reference",
            name="uq_payment_attempt_provider_payment_reference",
        ),
        sa.UniqueConstraint("telegram_invoice_payload", name="uq_payment_attempt_telegram_invoice_payload"),
        sa.UniqueConstraint("telegram_payment_charge_id", name="uq_payment_attempt_telegram_charge"),
        sa.Index(
            "uq_payment_attempt_one_active_order",
            "order_id",
            unique=True,
            postgresql_where=sa.text(ACTIVE_PAYMENT_ATTEMPT_SQL),
            sqlite_where=sa.text(ACTIVE_PAYMENT_ATTEMPT_SQL),
        ),
    )

    order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    buyer_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    method: Mapped[PaymentMethod] = mapped_column(
        sa.Enum(PaymentMethod, name="payment_method"), nullable=False, index=True
    )
    provider_namespace: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    status: Mapped[PaymentAttemptStatus] = mapped_column(
        sa.Enum(PaymentAttemptStatus, name="payment_attempt_status"),
        nullable=False,
        default=PaymentAttemptStatus.AWAITING_PAYMENT,
        server_default=PaymentAttemptStatus.AWAITING_PAYMENT.value,
        index=True,
    )
    order_amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    order_currency: Mapped[str] = mapped_column(
        sa.String(3), nullable=False, default="INR", server_default="INR"
    )
    provider_amount: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    provider_currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    method_data_snapshot: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False, default=dict)
    merchant_reference: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    provider_order_reference: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    provider_payment_reference: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    telegram_invoice_payload: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    manual_destination_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("manual_payment_destinations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    idempotency_record_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("idempotency_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, index=True)
    succeeded_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)


class PaymentProviderEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A callback retained before it may affect a payment attempt or order."""

    __tablename__ = "payment_provider_events"
    __table_args__ = (
        sa.UniqueConstraint(
            "provider_namespace", "external_event_id", name="uq_payment_provider_event_external"
        ),
        sa.UniqueConstraint("telegram_payment_charge_id", name="uq_payment_provider_event_telegram_charge"),
        sa.CheckConstraint(
            "provider_amount IS NULL OR provider_amount > 0",
            name="ck_payment_provider_event_amount_positive",
        ),
    )

    provider_namespace: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    external_event_id: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    payload_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    raw_payload: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    request_headers: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
    payment_attempt_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("payment_attempts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    merchant_reference: Mapped[str | None] = mapped_column(sa.String(160), nullable=True, index=True)
    provider_payment_reference: Mapped[str | None] = mapped_column(sa.String(160), nullable=True, index=True)
    provider_amount: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    provider_currency: Mapped[str | None] = mapped_column(sa.String(3), nullable=True)
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    signature_verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    status: Mapped[PaymentProviderEventStatus] = mapped_column(
        sa.Enum(PaymentProviderEventStatus, name="payment_provider_event_status"),
        nullable=False,
        default=PaymentProviderEventStatus.RECEIVED,
        server_default=PaymentProviderEventStatus.RECEIVED.value,
        index=True,
    )
    processing_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class ManualPaymentProof(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A user-provided UTR/proof awaiting an accountable manual decision."""

    __tablename__ = "manual_payment_proofs"
    __table_args__ = (
        sa.UniqueConstraint("utr", name="uq_manual_payment_proof_utr"),
        sa.CheckConstraint("submitted_amount_paise > 0", name="ck_manual_payment_proof_amount_positive"),
        sa.CheckConstraint("submitted_currency = 'INR'", name="ck_manual_payment_proof_currency_inr"),
        sa.CheckConstraint(
            "(status NOT IN ('APPROVED', 'REJECTED')) OR "
            "(reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL AND review_note IS NOT NULL)",
            name="ck_manual_payment_proof_terminal_review",
        ),
    )

    payment_attempt_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("payment_attempts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    manual_destination_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("manual_payment_destinations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    buyer_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    utr: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    proof_reference: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    proof_metadata: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
    submitted_amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    submitted_currency: Mapped[str] = mapped_column(
        sa.String(3), nullable=False, default="INR", server_default="INR"
    )
    submitted_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    status: Mapped[ManualPaymentProofStatus] = mapped_column(
        sa.Enum(ManualPaymentProofStatus, name="manual_payment_proof_status"),
        nullable=False,
        default=ManualPaymentProofStatus.SUBMITTED,
        server_default=ManualPaymentProofStatus.SUBMITTED.value,
        index=True,
    )
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)


class PaymentSettlement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The one-to-one bridge from trusted external evidence to an order."""

    __tablename__ = "payment_settlements"
    __table_args__ = (
        sa.UniqueConstraint("payment_attempt_id", name="uq_payment_settlement_attempt"),
        sa.UniqueConstraint("order_id", name="uq_payment_settlement_order"),
        sa.UniqueConstraint("journal_group_id", name="uq_payment_settlement_journal"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_payment_settlement_idempotency"),
        sa.UniqueConstraint("provider_event_id", name="uq_payment_settlement_provider_event"),
        sa.UniqueConstraint("manual_payment_proof_id", name="uq_payment_settlement_manual_proof"),
        sa.CheckConstraint("amount_paise > 0", name="ck_payment_settlement_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_payment_settlement_currency_inr"),
    )

    payment_attempt_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("payment_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    journal_group_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("journal_groups.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_record_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("idempotency_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_event_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("payment_provider_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    manual_payment_proof_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("manual_payment_proofs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    verification_source: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    settled_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    settled_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class PaymentOutboxEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable, deduplicated post-commit payment work."""

    __tablename__ = "payment_outbox_events"
    __table_args__ = (sa.UniqueConstraint("deduplication_key", name="uq_payment_outbox_deduplication"),)

    payment_attempt_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("payment_attempts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    aggregate_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False, index=True)
    deduplication_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    status: Mapped[PaymentOutboxEventStatus] = mapped_column(
        sa.Enum(PaymentOutboxEventStatus, name="payment_outbox_event_status"),
        nullable=False,
        default=PaymentOutboxEventStatus.PENDING,
        server_default=PaymentOutboxEventStatus.PENDING.value,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class PaymentReconciliationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A bounded provider-status reconciliation attempt with persisted outcome."""

    __tablename__ = "payment_reconciliation_runs"
    __table_args__ = (
        sa.UniqueConstraint("run_key", name="uq_payment_reconciliation_run_key"),
        sa.CheckConstraint(
            "window_ends_at IS NULL OR window_starts_at IS NULL OR window_ends_at >= window_starts_at",
            name="ck_payment_reconciliation_window_order",
        ),
    )

    run_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    provider_namespace: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    method: Mapped[PaymentMethod] = mapped_column(
        sa.Enum(PaymentMethod, name="payment_method"), nullable=False, index=True
    )
    status: Mapped[PaymentReconciliationStatus] = mapped_column(
        sa.Enum(PaymentReconciliationStatus, name="payment_reconciliation_status"),
        nullable=False,
        default=PaymentReconciliationStatus.PENDING,
        server_default=PaymentReconciliationStatus.PENDING.value,
        index=True,
    )
    window_starts_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    window_ends_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    checkpoint: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
    result_summary: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
    requested_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)


__all__ = [
    "ACTIVE_PAYMENT_ATTEMPT_SQL",
    "ACTIVE_PAYMENT_ATTEMPT_STATUSES",
    "ManualPaymentDestination",
    "ManualPaymentDestinationStatus",
    "ManualPaymentProof",
    "ManualPaymentProofStatus",
    "PaymentAttempt",
    "PaymentAttemptStatus",
    "PaymentMethod",
    "PaymentOutboxEvent",
    "PaymentOutboxEventStatus",
    "PaymentProviderEvent",
    "PaymentProviderEventStatus",
    "PaymentReconciliationRun",
    "PaymentReconciliationStatus",
    "PaymentSettlement",
    "P2PPaymentSubmission",
    "P2PSettlement",
    "P2PVerifiedPaymentReference",
]
