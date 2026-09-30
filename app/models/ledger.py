"""Immutable double-entry journal models for wallet accounting."""

from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin


PLATFORM_CLEARING_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")
PLATFORM_CLEARING_ACCOUNT_CODE = "platform:clearing:inr"
PLATFORM_P2P_ORDER_PENDING_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000002")
PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE = "platform:p2p-order-funds-pending:inr"
PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000003")
PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE = "platform:external-order-pending:inr"
PLATFORM_PRIZE_POOL_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000004")
PLATFORM_PRIZE_POOL_ACCOUNT_CODE = "platform:revenue:prize-pool:inr"
PLATFORM_MARKETING_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000005")
PLATFORM_MARKETING_ACCOUNT_CODE = "platform:revenue:marketing:inr"
PLATFORM_OPERATIONS_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000006")
PLATFORM_OPERATIONS_ACCOUNT_CODE = "platform:revenue:operations:inr"
PLATFORM_EMERGENCY_RESERVE_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000007")
PLATFORM_EMERGENCY_RESERVE_ACCOUNT_CODE = "platform:revenue:emergency-reserve:inr"
PLATFORM_PROFIT_GROWTH_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000008")
PLATFORM_PROFIT_GROWTH_ACCOUNT_CODE = "platform:revenue:profit-growth:inr"


class AccountKind(StrEnum):
    USER_AVAILABLE = "USER_AVAILABLE"
    USER_WITHDRAWAL_HELD = "USER_WITHDRAWAL_HELD"
    PLATFORM_CLEARING = "PLATFORM_CLEARING"
    PLATFORM_P2P_ORDER_PENDING = "PLATFORM_P2P_ORDER_PENDING"
    PLATFORM_EXTERNAL_ORDER_PENDING = "PLATFORM_EXTERNAL_ORDER_PENDING"
    PLATFORM_PRIZE_POOL = "PLATFORM_PRIZE_POOL"
    PLATFORM_MARKETING = "PLATFORM_MARKETING"
    PLATFORM_OPERATIONS = "PLATFORM_OPERATIONS"
    PLATFORM_EMERGENCY_RESERVE = "PLATFORM_EMERGENCY_RESERVE"
    PLATFORM_PROFIT_GROWTH = "PLATFORM_PROFIT_GROWTH"


class PostingDirection(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class LedgerAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ledger_accounts"
    __table_args__ = (
        sa.UniqueConstraint("wallet_id", "kind", name="uq_ledger_account_wallet_kind"),
        sa.CheckConstraint("currency = 'INR'", name="ck_ledger_account_currency_inr"),
    )

    code: Mapped[str] = mapped_column(sa.String(255), nullable=False, unique=True, index=True)
    kind: Mapped[AccountKind] = mapped_column(sa.Enum(AccountKind, name="ledger_account_kind"), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    wallet_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=True
    )
    user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    wallet: Mapped["Wallet | None"] = relationship(back_populates="accounts", lazy="selectin")
    postings: Mapped[list["JournalPosting"]] = relationship(back_populates="account", lazy="selectin")


class JournalGroup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "journal_groups"
    __table_args__ = (sa.CheckConstraint("currency = 'INR'", name="ck_journal_group_currency_inr"),)

    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    reference_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    reference_id: Mapped[UUID | None] = mapped_column(sa.Uuid(as_uuid=True), nullable=True)
    idempotency_record_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("idempotency_records.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    postings: Mapped[list["JournalPosting"]] = relationship(
        back_populates="journal_group", lazy="selectin", cascade="save-update, merge"
    )


class JournalPosting(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "journal_postings"
    __table_args__ = (
        sa.CheckConstraint("amount_paise > 0", name="ck_journal_posting_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_journal_posting_currency_inr"),
    )

    journal_group_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("journal_groups.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[PostingDirection] = mapped_column(
        sa.Enum(PostingDirection, name="posting_direction"), nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    journal_group: Mapped[JournalGroup] = relationship(back_populates="postings", lazy="selectin")
    account: Mapped[LedgerAccount] = relationship(back_populates="postings", lazy="selectin")
