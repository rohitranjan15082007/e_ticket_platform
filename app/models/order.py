"""Orders, immutable item snapshots, and ticket reservations."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    WAITING_FOR_MATCH = "WAITING_FOR_MATCH"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    PAYMENT_REVIEW = "PAYMENT_REVIEW"
    PAID = "PAID"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"


class DeliveryStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class TicketProductType(StrEnum):
    SERIES = "SERIES"
    PACKAGE = "PACKAGE"


class TicketReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    ALLOCATED = "ALLOCATED"
    RELEASED = "RELEASED"


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One buyer checkout with a server-frozen total and reservation expiry."""

    __tablename__ = "orders"
    __table_args__ = (
        sa.CheckConstraint("total_paise > 0", name="ck_order_total_positive"),
        sa.CheckConstraint("subtotal_paise > 0", name="ck_order_subtotal_positive"),
        sa.CheckConstraint("discount_paise >= 0", name="ck_order_discount_nonnegative"),
        sa.CheckConstraint(
            "subtotal_paise = total_paise + discount_paise",
            name="ck_order_amount_snapshot_matches",
        ),
        sa.CheckConstraint("currency = 'INR'", name="ck_order_currency_inr"),
        sa.CheckConstraint(
            "coupon_code_snapshot IS NULL OR coupon_code_snapshot = upper(coupon_code_snapshot)",
            name="ck_order_coupon_code_uppercase",
        ),
    )

    buyer_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[OrderStatus] = mapped_column(
        sa.Enum(OrderStatus, name="order_status"),
        nullable=False,
        default=OrderStatus.PENDING_PAYMENT,
        server_default=OrderStatus.PENDING_PAYMENT.value,
        index=True,
    )
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        sa.Enum(DeliveryStatus, name="delivery_status"),
        nullable=False,
        default=DeliveryStatus.NOT_STARTED,
        server_default=DeliveryStatus.NOT_STARTED.value,
        index=True,
    )
    subtotal_paise: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    discount_paise: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    total_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    coupon_code_snapshot: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, index=True)
    settlement_reference_id: Mapped[UUID | None] = mapped_column(sa.Uuid(as_uuid=True), nullable=True, unique=True)
    settled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", lazy="selectin", cascade="save-update, merge"
    )
    reservations: Mapped[list["TicketReservation"]] = relationship(
        back_populates="order", lazy="selectin", cascade="save-update, merge"
    )
    coupon_redemption: Mapped["CouponRedemption | None"] = relationship(
        back_populates="order", lazy="selectin", uselist=False
    )


@sa.event.listens_for(Order, "before_insert")
def _set_legacy_order_money_snapshot(_: object, __: object, target: Order) -> None:
    """Give legacy internal ORM callers a no-discount frozen snapshot.

    Application writes always set these values explicitly. Direct database
    writes still have to satisfy the database constraints.
    """

    if target.discount_paise is None:
        target.discount_paise = 0
    if target.subtotal_paise is None or target.subtotal_paise <= 0:
        target.subtotal_paise = target.total_paise + target.discount_paise


class OrderItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable product/price snapshots; ``product_id`` is deliberately polymorphic."""

    __tablename__ = "order_items"
    __table_args__ = (
        sa.CheckConstraint("unit_price_paise > 0", name="ck_order_item_unit_price_positive"),
        sa.CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        sa.CheckConstraint("line_total_paise > 0", name="ck_order_item_total_positive"),
        sa.CheckConstraint(
            "line_total_paise = unit_price_paise * quantity", name="ck_order_item_total_matches"
        ),
        sa.CheckConstraint("currency = 'INR'", name="ck_order_item_currency_inr"),
    )

    order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_type: Mapped[TicketProductType] = mapped_column(
        sa.Enum(TicketProductType, name="ticket_product_type"), nullable=False
    )
    product_id: Mapped[UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False, index=True)
    product_name_snapshot: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    line_total_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    package_contents_snapshot: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
    order: Mapped[Order] = relationship(back_populates="items")
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="order_item", lazy="selectin")
    reservations: Mapped[list["TicketReservation"]] = relationship(
        back_populates="order_item", lazy="selectin"
    )


class TicketReservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A finite inventory claim until allocation or a controlled release."""

    __tablename__ = "ticket_reservations"
    __table_args__ = (
        sa.CheckConstraint("quantity > 0", name="ck_ticket_reservation_quantity_positive"),
        sa.UniqueConstraint("order_id", "series_id", name="uq_ticket_reservation_order_series"),
    )

    order_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_item_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    series_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("ticket_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[TicketReservationStatus] = mapped_column(
        sa.Enum(TicketReservationStatus, name="ticket_reservation_status"),
        nullable=False,
        default=TicketReservationStatus.RESERVED,
        server_default=TicketReservationStatus.RESERVED.value,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, index=True)
    allocated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    order: Mapped[Order] = relationship(back_populates="reservations")
    order_item: Mapped[OrderItem] = relationship(back_populates="reservations")
    series: Mapped["TicketSeries"] = relationship(back_populates="reservations")
