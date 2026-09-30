"""Immutable settlement-linked revenue-allocation records."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class RevenueAllocationSource(StrEnum):
    """The pending-funds account that a trusted settlement created."""

    EXTERNAL_ORDER_PENDING = "EXTERNAL_ORDER_PENDING"
    P2P_ORDER_PENDING = "P2P_ORDER_PENDING"


class RevenueAllocation(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One non-recalculable integer-paise split for one settled order."""

    __tablename__ = "revenue_allocations"
    __table_args__ = (
        sa.CheckConstraint("currency = 'INR'", name="ck_revenue_allocation_currency_inr"),
        sa.CheckConstraint("allocation_base_paise > 0", name="ck_revenue_allocation_base_positive"),
        sa.CheckConstraint("prize_pool_paise >= 0", name="ck_revenue_allocation_prize_nonnegative"),
        sa.CheckConstraint("marketing_paise >= 0", name="ck_revenue_allocation_marketing_nonnegative"),
        sa.CheckConstraint("operations_paise >= 0", name="ck_revenue_allocation_operations_nonnegative"),
        sa.CheckConstraint("reserve_paise >= 0", name="ck_revenue_allocation_reserve_nonnegative"),
        sa.CheckConstraint("profit_growth_paise >= 0", name="ck_revenue_allocation_profit_nonnegative"),
        sa.CheckConstraint(
            "allocation_base_paise = prize_pool_paise + marketing_paise + operations_paise "
            "+ reserve_paise + profit_growth_paise",
            name="ck_revenue_allocation_amounts_match",
        ),
        sa.UniqueConstraint("order_id", name="uq_revenue_allocation_order"),
        sa.UniqueConstraint("settlement_reference_id", name="uq_revenue_allocation_settlement"),
        sa.UniqueConstraint("journal_group_id", name="uq_revenue_allocation_journal_group"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_revenue_allocation_idempotency"),
    )

    order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    settlement_reference_id: Mapped[UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False, index=True)
    source: Mapped[RevenueAllocationSource] = mapped_column(
        sa.Enum(RevenueAllocationSource, name="revenue_allocation_source"), nullable=False, index=True
    )
    source_account_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    journal_group_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("journal_groups.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_record_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("idempotency_records.id", ondelete="RESTRICT"), nullable=False
    )
    allocation_base_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    prize_pool_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    marketing_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    operations_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    reserve_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    profit_growth_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    allocated_by_user_id: Mapped[UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    allocated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, index=True)
