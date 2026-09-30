"""Ticket-series catalog, inventory counters, and prize configuration."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TicketSeriesStatus(StrEnum):
    """Lifecycle states for a finite ticket series."""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    DRAWN = "DRAWN"
    RESULT_PUBLISHED = "RESULT_PUBLISHED"
    CANCELLED = "CANCELLED"


class TicketSeries(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An INR-denominated run of limited tickets."""

    __tablename__ = "ticket_series"
    __table_args__ = (
        sa.CheckConstraint("price_paise > 0", name="ck_ticket_series_price_positive"),
        sa.CheckConstraint("ticket_limit > 0", name="ck_ticket_series_limit_positive"),
        sa.CheckConstraint("sold_count >= 0", name="ck_ticket_series_sold_nonnegative"),
        sa.CheckConstraint("reserved_count >= 0", name="ck_ticket_series_reserved_nonnegative"),
        sa.CheckConstraint(
            "sold_count + reserved_count <= ticket_limit",
            name="ck_ticket_series_inventory_within_limit",
        ),
        sa.CheckConstraint("currency = 'INR'", name="ck_ticket_series_currency_inr"),
        sa.CheckConstraint("sales_end_at > sales_start_at", name="ck_ticket_series_sales_window"),
        sa.CheckConstraint("draw_at > sales_end_at", name="ck_ticket_series_draw_after_sales"),
    )

    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    price_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    ticket_limit: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    sold_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    reserved_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    sales_start_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    sales_end_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    draw_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    status: Mapped[TicketSeriesStatus] = mapped_column(
        sa.Enum(TicketSeriesStatus, name="ticket_series_status"),
        nullable=False,
        default=TicketSeriesStatus.DRAFT,
        server_default=TicketSeriesStatus.DRAFT.value,
        index=True,
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    prizes: Mapped[list["TicketSeriesPrize"]] = relationship(
        back_populates="series", lazy="selectin", cascade="save-update, merge"
    )
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="series", lazy="selectin")
    reservations: Mapped[list["TicketReservation"]] = relationship(back_populates="series", lazy="selectin")
    package_items: Mapped[list["TicketPackageItem"]] = relationship(
        back_populates="series", lazy="selectin"
    )


class TicketSeriesPrize(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Prize configuration frozen before a series can be opened."""

    __tablename__ = "ticket_series_prizes"
    __table_args__ = (
        sa.CheckConstraint("rank > 0", name="ck_ticket_series_prize_rank_positive"),
        sa.CheckConstraint("prize_paise > 0", name="ck_ticket_series_prize_amount_positive"),
        sa.UniqueConstraint("series_id", "rank", name="uq_ticket_series_prize_rank"),
    )

    series_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("ticket_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    rank: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    title: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    prize_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    series: Mapped[TicketSeries] = relationship(back_populates="prizes")
