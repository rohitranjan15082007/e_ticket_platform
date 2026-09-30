"""Allocated ticket entitlements with immutable per-series serial numbers."""

from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TicketStatus(StrEnum):
    ALLOCATED = "ALLOCATED"
    VOID = "VOID"


class Ticket(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A ticket is created only by post-settlement allocation."""

    __tablename__ = "tickets"
    __table_args__ = (
        sa.CheckConstraint("serial_number > 0", name="ck_ticket_serial_positive"),
        sa.CheckConstraint("prize_paise >= 0", name="ck_ticket_prize_nonnegative"),
        sa.UniqueConstraint("series_id", "serial_number", name="uq_ticket_series_serial"),
    )

    series_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("ticket_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    serial_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    order_item_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[TicketStatus] = mapped_column(
        sa.Enum(TicketStatus, name="ticket_status"),
        nullable=False,
        default=TicketStatus.ALLOCATED,
        server_default=TicketStatus.ALLOCATED.value,
        index=True,
    )
    is_winner: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default=sa.false())
    prize_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default="0")
    series: Mapped["TicketSeries"] = relationship(back_populates="tickets")
    order_item: Mapped["OrderItem"] = relationship(back_populates="tickets")
