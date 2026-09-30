"""Affiliate account, privacy-minimal click, conversion, and commission records."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AffiliateStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class AffiliateCommissionType(StrEnum):
    FIXED_PAISE = "FIXED_PAISE"
    PERCENT_BPS = "PERCENT_BPS"


class AffiliateConversionStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    VOIDED = "VOIDED"


class AffiliateCommissionStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    VOIDED = "VOIDED"


class Affiliate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An administrator-approved account whose policy is frozen per conversion."""

    __tablename__ = "affiliates"
    __table_args__ = (
        sa.CheckConstraint("code = upper(code)", name="ck_affiliate_code_uppercase"),
        sa.CheckConstraint("minimum_order_paise > 0", name="ck_affiliate_minimum_positive"),
        sa.CheckConstraint(
            "("
            "commission_type = 'FIXED_PAISE' "
            "AND fixed_commission_paise IS NOT NULL AND fixed_commission_paise > 0 "
            "AND percentage_bps IS NULL AND max_commission_paise IS NULL"
            ") OR ("
            "commission_type = 'PERCENT_BPS' "
            "AND percentage_bps IS NOT NULL AND percentage_bps > 0 AND percentage_bps <= 10000 "
            "AND fixed_commission_paise IS NULL "
            "AND (max_commission_paise IS NULL OR max_commission_paise > 0)"
            ")",
            name="ck_affiliate_commission_definition",
        ),
    )

    code: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    owner_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, unique=True
    )
    commission_type: Mapped[AffiliateCommissionType] = mapped_column(
        sa.Enum(AffiliateCommissionType, name="affiliate_commission_type"), nullable=False
    )
    fixed_commission_paise: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    percentage_bps: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    max_commission_paise: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    minimum_order_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    status: Mapped[AffiliateStatus] = mapped_column(
        sa.Enum(AffiliateStatus, name="affiliate_status"),
        nullable=False,
        default=AffiliateStatus.PENDING,
        server_default=AffiliateStatus.PENDING.value,
        index=True,
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    activated_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class AffiliateClick(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Deduplicated click evidence without raw IP address or browser fingerprint."""

    __tablename__ = "affiliate_clicks"
    __table_args__ = (sa.UniqueConstraint("click_reference", name="uq_affiliate_click_reference"),)

    affiliate_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("affiliates.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    click_reference: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    landing_path: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, index=True)


class AffiliateConversion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Evidence-backed admin attribution of one settled order to one affiliate."""

    __tablename__ = "affiliate_conversions"
    __table_args__ = (
        sa.CheckConstraint(
            "(status = 'PENDING_REVIEW' AND voided_at IS NULL) OR "
            "(status = 'VOIDED' AND voided_at IS NOT NULL)",
            name="ck_affiliate_conversion_status_timestamps",
        ),
        sa.UniqueConstraint("order_id", name="uq_affiliate_conversion_order"),
    )

    affiliate_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("affiliates.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    buyer_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    affiliate_code_snapshot: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    policy_snapshot: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    evidence_reference: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[AffiliateConversionStatus] = mapped_column(
        sa.Enum(AffiliateConversionStatus, name="affiliate_conversion_status"),
        nullable=False,
        default=AffiliateConversionStatus.PENDING_REVIEW,
        server_default=AffiliateConversionStatus.PENDING_REVIEW.value,
        index=True,
    )
    created_from_settlement_id: Mapped[UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    attributed_by_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    voided_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    voided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)


class AffiliateCommission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A pending non-wallet commission linked one-to-one to its conversion."""

    __tablename__ = "affiliate_commissions"
    __table_args__ = (
        sa.CheckConstraint("amount_paise > 0", name="ck_affiliate_commission_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_affiliate_commission_currency_inr"),
        sa.CheckConstraint(
            "(status = 'PENDING_REVIEW' AND voided_at IS NULL) OR "
            "(status = 'VOIDED' AND voided_at IS NOT NULL)",
            name="ck_affiliate_commission_status_timestamps",
        ),
        sa.UniqueConstraint("conversion_id", name="uq_affiliate_commission_conversion"),
    )

    conversion_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("affiliate_conversions.id", ondelete="RESTRICT"), nullable=False
    )
    affiliate_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("affiliates.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    beneficiary_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    status: Mapped[AffiliateCommissionStatus] = mapped_column(
        sa.Enum(AffiliateCommissionStatus, name="affiliate_commission_status"),
        nullable=False,
        default=AffiliateCommissionStatus.PENDING_REVIEW,
        server_default=AffiliateCommissionStatus.PENDING_REVIEW.value,
        index=True,
    )
    voided_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    voided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
