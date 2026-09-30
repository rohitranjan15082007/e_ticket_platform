"""Referral program, attribution, and pending-reward persistence."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ReferralProgramStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ReferralStatus(StrEnum):
    PENDING = "PENDING"
    QUALIFIED = "QUALIFIED"
    VOIDED = "VOIDED"


class ReferralRewardRecipient(StrEnum):
    REFERRER = "REFERRER"
    REFERRED = "REFERRED"


class ReferralRewardStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    VOIDED = "VOIDED"


class ReferralProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A user-owned non-secret code which can be claimed once by another user."""

    __tablename__ = "referral_profiles"
    __table_args__ = (
        sa.CheckConstraint("code = upper(code)", name="ck_referral_profile_code_uppercase"),
        sa.UniqueConstraint("user_id", name="uq_referral_profile_user"),
        sa.UniqueConstraint("code", name="uq_referral_profile_code"),
    )

    user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true(), index=True
    )


class ReferralProgram(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Administrator-configured fixed rewards for one qualifying first order."""

    __tablename__ = "referral_programs"
    __table_args__ = (
        sa.CheckConstraint("referrer_reward_paise > 0", name="ck_referral_program_referrer_positive"),
        sa.CheckConstraint("referred_reward_paise > 0", name="ck_referral_program_referred_positive"),
        sa.CheckConstraint("minimum_order_paise > 0", name="ck_referral_program_minimum_positive"),
        sa.CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at",
            name="ck_referral_program_window",
        ),
        sa.Index(
            "uq_referral_program_one_active",
            "status",
            unique=True,
            postgresql_where=sa.text("status = 'ACTIVE'"),
            sqlite_where=sa.text("status = 'ACTIVE'"),
        ),
    )

    name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    referrer_reward_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    referred_reward_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    minimum_order_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    status: Mapped[ReferralProgramStatus] = mapped_column(
        sa.Enum(ReferralProgramStatus, name="referral_program_status"),
        nullable=False,
        default=ReferralProgramStatus.DRAFT,
        server_default=ReferralProgramStatus.DRAFT.value,
        index=True,
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    activated_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class Referral(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One immutable referrer/referred relationship under a frozen program."""

    __tablename__ = "referrals"
    __table_args__ = (
        sa.CheckConstraint(
            "("
            "status = 'PENDING' AND qualifying_order_id IS NULL "
            "AND qualified_at IS NULL AND voided_at IS NULL"
            ") OR ("
            "status = 'QUALIFIED' AND qualifying_order_id IS NOT NULL "
            "AND qualified_at IS NOT NULL AND voided_at IS NULL"
            ") OR ("
            "status = 'VOIDED' AND voided_at IS NOT NULL"
            ")",
            name="ck_referral_status_timestamps",
        ),
        sa.UniqueConstraint("referred_user_id", name="uq_referral_referred_user"),
        sa.UniqueConstraint("qualifying_order_id", name="uq_referral_qualifying_order"),
    )

    program_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("referral_programs.id", ondelete="RESTRICT"), nullable=False
    )
    referrer_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    referred_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    referral_code_snapshot: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    program_snapshot: Mapped[dict[str, object]] = mapped_column(sa.JSON, nullable=False)
    status: Mapped[ReferralStatus] = mapped_column(
        sa.Enum(ReferralStatus, name="referral_status"),
        nullable=False,
        default=ReferralStatus.PENDING,
        server_default=ReferralStatus.PENDING.value,
        index=True,
    )
    qualifying_order_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True
    )
    claimed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    qualified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)


class ReferralReward(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A non-wallet pending reward created only by a qualifying settlement."""

    __tablename__ = "referral_rewards"
    __table_args__ = (
        sa.CheckConstraint("amount_paise > 0", name="ck_referral_reward_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_referral_reward_currency_inr"),
        sa.CheckConstraint(
            "(status = 'PENDING_REVIEW' AND voided_at IS NULL) OR "
            "(status = 'VOIDED' AND voided_at IS NOT NULL)",
            name="ck_referral_reward_status_timestamps",
        ),
        sa.UniqueConstraint("referral_id", "recipient", name="uq_referral_reward_recipient"),
    )

    referral_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("referrals.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    qualifying_order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    recipient_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    recipient: Mapped[ReferralRewardRecipient] = mapped_column(
        sa.Enum(ReferralRewardRecipient, name="referral_reward_recipient"), nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    status: Mapped[ReferralRewardStatus] = mapped_column(
        sa.Enum(ReferralRewardStatus, name="referral_reward_status"),
        nullable=False,
        default=ReferralRewardStatus.PENDING_REVIEW,
        server_default=ReferralRewardStatus.PENDING_REVIEW.value,
        index=True,
    )
    created_from_settlement_id: Mapped[UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    voided_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    voided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
