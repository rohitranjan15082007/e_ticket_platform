"""Multi-series ticket packages and their finite inventory."""

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TicketPackage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A purchasable bundle whose contents are server-owned catalog records."""

    __tablename__ = "ticket_packages"
    __table_args__ = (
        sa.CheckConstraint("price_paise > 0", name="ck_ticket_package_price_positive"),
        sa.CheckConstraint(
            "inventory_limit IS NULL OR inventory_limit > 0", name="ck_ticket_package_limit_positive"
        ),
        sa.CheckConstraint("sold_count >= 0", name="ck_ticket_package_sold_nonnegative"),
        sa.CheckConstraint("reserved_count >= 0", name="ck_ticket_package_reserved_nonnegative"),
        sa.CheckConstraint(
            "inventory_limit IS NULL OR sold_count + reserved_count <= inventory_limit",
            name="ck_ticket_package_inventory_within_limit",
        ),
        sa.CheckConstraint("currency = 'INR'", name="ck_ticket_package_currency_inr"),
    )

    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    price_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    inventory_limit: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    sold_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    reserved_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="INR", server_default="INR")
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True, server_default=sa.true())
    created_by_user_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    items: Mapped[list["TicketPackageItem"]] = relationship(
        back_populates="package", lazy="selectin", cascade="save-update, merge"
    )


class TicketPackageItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One included quantity from one series; duplicates are forbidden."""

    __tablename__ = "ticket_package_items"
    __table_args__ = (
        sa.CheckConstraint("quantity > 0", name="ck_ticket_package_item_quantity_positive"),
        sa.UniqueConstraint("package_id", "series_id", name="uq_ticket_package_item_series"),
    )

    package_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("ticket_packages.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    series_id: Mapped[UUID] = mapped_column(
        sa.Uuid(as_uuid=True), sa.ForeignKey("ticket_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    package: Mapped[TicketPackage] = relationship(back_populates="items")
    series: Mapped["TicketSeries"] = relationship(back_populates="package_items", lazy="selectin")
