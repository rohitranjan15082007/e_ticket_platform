"""Wallet balances cached from immutable journal postings."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Wallet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "wallets"
    __table_args__ = (
        sa.CheckConstraint("available_paise >= 0", name="ck_wallet_available_nonnegative"),
        sa.CheckConstraint("locked_paise >= 0", name="ck_wallet_locked_nonnegative"),
        sa.CheckConstraint("currency = 'INR'", name="ck_wallet_currency_inr"),
    )

    user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR")
    available_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default="0")
    locked_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default="0")
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    accounts: Mapped[list["LedgerAccount"]] = relationship(
        back_populates="wallet", lazy="selectin", cascade="save-update, merge"
    )


class WalletHoldStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    SETTLED = "SETTLED"


class WalletHold(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named, one-time wallet reservation backing a future business workflow."""

    __tablename__ = "wallet_holds"
    __table_args__ = (
        sa.CheckConstraint("amount_paise > 0", name="ck_wallet_hold_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_wallet_hold_currency_inr"),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND release_journal_group_id IS NULL AND released_at IS NULL "
            "AND settlement_journal_group_id IS NULL AND settled_at IS NULL) "
            "OR (status = 'RELEASED' AND release_journal_group_id IS NOT NULL AND released_at IS NOT NULL "
            "AND settlement_journal_group_id IS NULL AND settled_at IS NULL) "
            "OR (status = 'SETTLED' AND settlement_journal_group_id IS NOT NULL AND settled_at IS NOT NULL "
            "AND release_journal_group_id IS NULL AND released_at IS NULL)",
            name="ck_wallet_hold_resolution",
        ),
        sa.UniqueConstraint("wallet_id", "reference_type", "reference_id", name="uq_wallet_hold_reference"),
        sa.UniqueConstraint("hold_journal_group_id", name="uq_wallet_hold_journal"),
        sa.UniqueConstraint("release_journal_group_id", name="uq_wallet_hold_release_journal"),
        sa.UniqueConstraint("settlement_journal_group_id", name="uq_wallet_hold_settlement_journal"),
    )

    wallet_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reference_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    reference_id: Mapped[UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False)
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    status: Mapped[WalletHoldStatus] = mapped_column(
        sa.Enum(WalletHoldStatus, name="wallet_hold_status"), nullable=False, default=WalletHoldStatus.ACTIVE
    )
    hold_journal_group_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("journal_groups.id", ondelete="RESTRICT"), nullable=False
    )
    release_journal_group_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("journal_groups.id", ondelete="RESTRICT"), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    settlement_journal_group_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("journal_groups.id", ondelete="RESTRICT"), nullable=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
