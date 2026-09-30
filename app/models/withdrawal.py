"""P2P withdrawal requests and verified receiver payment destinations."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PaymentDestinationStatus(StrEnum):
    """A destination is never exposed unless a controlled process verified it."""

    PENDING_REVIEW = "PENDING_REVIEW"
    VERIFIED = "VERIFIED"
    DISABLED = "DISABLED"


class PaymentDestination(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Receiver-owned, separately verified destination data.

    Phase 4 intentionally has no user endpoint that marks a new destination as
    verified. A later profile/KYC workflow must supply the controlled
    verification record before this can be selected for a withdrawal.
    """

    __tablename__ = "payment_destinations"
    __table_args__ = (
        sa.CheckConstraint(
            "(status <> 'VERIFIED') OR "
            "(verification_method IS NOT NULL AND verification_evidence_reference IS NOT NULL "
            "AND verified_at IS NOT NULL)",
            name="ck_payment_destination_verified_evidence",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider_namespace: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    display_label: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    destination_data: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    status: Mapped[PaymentDestinationStatus] = mapped_column(
        sa.Enum(PaymentDestinationStatus, name="payment_destination_status"),
        nullable=False,
        default=PaymentDestinationStatus.PENDING_REVIEW,
        server_default=PaymentDestinationStatus.PENDING_REVIEW.value,
        index=True,
    )
    verification_method: Mapped[str | None] = mapped_column(sa.String(100), nullable=True)
    verification_evidence_reference: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class WithdrawalStatus(StrEnum):
    WAITING_FOR_BUYER = "WAITING_FOR_BUYER"
    MATCHED = "MATCHED"
    UNDER_REVIEW = "UNDER_REVIEW"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


UNRESOLVED_WITHDRAWAL_STATUSES = (
    WithdrawalStatus.WAITING_FOR_BUYER.value,
    WithdrawalStatus.MATCHED.value,
    WithdrawalStatus.UNDER_REVIEW.value,
)


class WithdrawalRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single, snapshotted and named-held P2P withdrawal request."""

    __tablename__ = "withdrawal_requests"
    __table_args__ = (
        sa.CheckConstraint("amount_paise > 0", name="ck_withdrawal_request_amount_positive"),
        sa.CheckConstraint("eligible_balance_snapshot_paise >= 0", name="ck_withdrawal_eligible_nonnegative"),
        sa.CheckConstraint("max_amount_snapshot_paise >= 0", name="ck_withdrawal_max_nonnegative"),
        sa.CheckConstraint("amount_paise <= max_amount_snapshot_paise", name="ck_withdrawal_within_snapshot"),
        sa.CheckConstraint("currency = 'INR'", name="ck_withdrawal_currency_inr"),
        sa.UniqueConstraint("wallet_hold_id", name="uq_withdrawal_wallet_hold"),
        sa.Index(
            "uq_withdrawal_one_unresolved_user",
            "user_id",
            unique=True,
            postgresql_where=sa.text("status IN ('WAITING_FOR_BUYER', 'MATCHED', 'UNDER_REVIEW')"),
            sqlite_where=sa.text("status IN ('WAITING_FOR_BUYER', 'MATCHED', 'UNDER_REVIEW')"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    wallet_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payment_destination_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("payment_destinations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    wallet_hold_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("wallet_holds.id", ondelete="RESTRICT"), nullable=True
    )
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    eligible_balance_snapshot_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    max_amount_snapshot_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    rule_snapshot: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    rule_version: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    status: Mapped[WithdrawalStatus] = mapped_column(
        sa.Enum(WithdrawalStatus, name="withdrawal_status"),
        nullable=False,
        default=WithdrawalStatus.WAITING_FOR_BUYER,
        server_default=WithdrawalStatus.WAITING_FOR_BUYER.value,
        index=True,
    )
    matched_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
