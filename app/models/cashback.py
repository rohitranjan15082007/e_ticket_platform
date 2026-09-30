"""Cashback campaign configuration and non-wallet pending rewards."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CashbackCampaignStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class CashbackRewardType(StrEnum):
    FIXED_PAISE = "FIXED_PAISE"
    PERCENT_BPS = "PERCENT_BPS"


class CashbackRewardStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    VOIDED = "VOIDED"


class CashbackCampaign(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Only one campaign may be ACTIVE, preventing unpriced reward stacking."""

    __tablename__ = "cashback_campaigns"
    __table_args__ = (
        sa.CheckConstraint("code = upper(code)", name="ck_cashback_campaign_code_uppercase"),
        sa.CheckConstraint("minimum_order_paise > 0", name="ck_cashback_campaign_minimum_positive"),
        sa.CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at",
            name="ck_cashback_campaign_window",
        ),
        sa.CheckConstraint(
            "("
            "reward_type = 'FIXED_PAISE' "
            "AND fixed_reward_paise IS NOT NULL AND fixed_reward_paise > 0 "
            "AND percentage_bps IS NULL AND max_reward_paise IS NULL"
            ") OR ("
            "reward_type = 'PERCENT_BPS' "
            "AND percentage_bps IS NOT NULL AND percentage_bps > 0 AND percentage_bps <= 10000 "
            "AND fixed_reward_paise IS NULL "
            "AND (max_reward_paise IS NULL OR max_reward_paise > 0)"
            ")",
            name="ck_cashback_campaign_definition",
        ),
        sa.Index(
            "uq_cashback_campaign_one_active",
            "status",
            unique=True,
            postgresql_where=sa.text("status = 'ACTIVE'"),
            sqlite_where=sa.text("status = 'ACTIVE'"),
        ),
    )

    code: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    reward_type: Mapped[CashbackRewardType] = mapped_column(
        sa.Enum(CashbackRewardType, name="cashback_reward_type"), nullable=False
    )
    fixed_reward_paise: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    percentage_bps: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    max_reward_paise: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    minimum_order_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    status: Mapped[CashbackCampaignStatus] = mapped_column(
        sa.Enum(CashbackCampaignStatus, name="cashback_campaign_status"),
        nullable=False,
        default=CashbackCampaignStatus.DRAFT,
        server_default=CashbackCampaignStatus.DRAFT.value,
        index=True,
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    activated_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class CashbackReward(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Settlement-created pending reward, intentionally not a wallet credit."""

    __tablename__ = "cashback_rewards"
    __table_args__ = (
        sa.CheckConstraint("order_base_paise > 0", name="ck_cashback_reward_base_positive"),
        sa.CheckConstraint("amount_paise > 0", name="ck_cashback_reward_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_cashback_reward_currency_inr"),
        sa.CheckConstraint(
            "(status = 'PENDING_REVIEW' AND voided_at IS NULL) OR "
            "(status = 'VOIDED' AND voided_at IS NOT NULL)",
            name="ck_cashback_reward_status_timestamps",
        ),
        sa.UniqueConstraint("campaign_id", "order_id", name="uq_cashback_reward_campaign_order"),
    )

    campaign_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("cashback_campaigns.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    buyer_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    rule_snapshot: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    order_base_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    status: Mapped[CashbackRewardStatus] = mapped_column(
        sa.Enum(CashbackRewardStatus, name="cashback_reward_status"),
        nullable=False,
        default=CashbackRewardStatus.PENDING_REVIEW,
        server_default=CashbackRewardStatus.PENDING_REVIEW.value,
        index=True,
    )
    created_from_settlement_id: Mapped[UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    voided_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    voided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
