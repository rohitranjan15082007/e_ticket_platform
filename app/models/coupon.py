"""Coupon rules and immutable checkout redemption snapshots."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CouponDiscountType(StrEnum):
    """The two server-calculated discount forms supported at checkout."""

    FIXED_PAISE = "FIXED_PAISE"
    PERCENT_BPS = "PERCENT_BPS"


class CouponRedemptionStatus(StrEnum):
    """A checkout either holds, consumes, or releases one coupon use."""

    RESERVED = "RESERVED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class Coupon(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Administrator-owned coupon policy.

    active_redemption_count counts both reserved and consumed redemptions.
    It is changed only while the coupon row is locked, so global usage limits
    include unsettled checkouts without a count-then-insert race.
    """

    __tablename__ = "coupons"
    __table_args__ = (
        sa.CheckConstraint("code = upper(code)", name="ck_coupon_code_uppercase"),
        sa.CheckConstraint("minimum_order_paise >= 0", name="ck_coupon_minimum_nonnegative"),
        sa.CheckConstraint("active_redemption_count >= 0", name="ck_coupon_active_count_nonnegative"),
        sa.CheckConstraint("usage_limit IS NULL OR usage_limit > 0", name="ck_coupon_usage_limit_positive"),
        sa.CheckConstraint(
            "per_user_limit IS NULL OR per_user_limit > 0", name="ck_coupon_per_user_limit_positive"
        ),
        sa.CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at",
            name="ck_coupon_validity_window",
        ),
        sa.CheckConstraint(
            "("
            "discount_type = 'FIXED_PAISE' "
            "AND fixed_discount_paise IS NOT NULL AND fixed_discount_paise > 0 "
            "AND percentage_bps IS NULL AND max_discount_paise IS NULL"
            ") OR ("
            "discount_type = 'PERCENT_BPS' "
            "AND percentage_bps IS NOT NULL AND percentage_bps > 0 AND percentage_bps <= 10000 "
            "AND fixed_discount_paise IS NULL "
            "AND (max_discount_paise IS NULL OR max_discount_paise > 0)"
            ")",
            name="ck_coupon_discount_definition",
        ),
    )

    code: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    discount_type: Mapped[CouponDiscountType] = mapped_column(
        sa.Enum(CouponDiscountType, name="coupon_discount_type"), nullable=False
    )
    fixed_discount_paise: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    percentage_bps: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    max_discount_paise: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    minimum_order_paise: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    usage_limit: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    per_user_limit: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    active_redemption_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    starts_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true(), index=True
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    redemptions: Mapped[list["CouponRedemption"]] = relationship(
        back_populates="coupon", lazy="selectin", cascade="save-update, merge"
    )


class CouponRedemption(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Frozen discount evidence bound one-to-one to the order it priced."""

    __tablename__ = "coupon_redemptions"
    __table_args__ = (
        sa.CheckConstraint("gross_paise > 0", name="ck_coupon_redemption_gross_positive"),
        sa.CheckConstraint("discount_paise > 0", name="ck_coupon_redemption_discount_positive"),
        sa.CheckConstraint("net_paise > 0", name="ck_coupon_redemption_net_positive"),
        sa.CheckConstraint(
            "gross_paise = discount_paise + net_paise",
            name="ck_coupon_redemption_amounts_match",
        ),
        sa.CheckConstraint(
            "("
            "status = 'RESERVED' AND consumed_at IS NULL AND released_at IS NULL"
            ") OR ("
            "status = 'CONSUMED' AND consumed_at IS NOT NULL AND released_at IS NULL"
            ") OR ("
            "status = 'RELEASED' AND released_at IS NOT NULL AND consumed_at IS NULL"
            ")",
            name="ck_coupon_redemption_status_timestamps",
        ),
        sa.UniqueConstraint("order_id", name="uq_coupon_redemption_order"),
    )

    coupon_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("coupons.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    buyer_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    coupon_code_snapshot: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    rule_snapshot: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    gross_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    discount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    net_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    status: Mapped[CouponRedemptionStatus] = mapped_column(
        sa.Enum(CouponRedemptionStatus, name="coupon_redemption_status"),
        nullable=False,
        default=CouponRedemptionStatus.RESERVED,
        server_default=CouponRedemptionStatus.RESERVED.value,
        index=True,
    )
    reserved_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    coupon: Mapped[Coupon] = relationship(back_populates="redemptions")
    order: Mapped["Order"] = relationship(back_populates="coupon_redemption")
