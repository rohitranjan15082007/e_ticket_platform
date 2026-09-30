"""create ticket catalog, packages, orders, reservations and tickets

Revision ID: 0004_ticket_catalog_orders
Revises: 0003_financial_integrity
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_ticket_catalog_orders"
down_revision = "0003_financial_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ticket_series_status = sa.Enum(
        "DRAFT", "PUBLISHED", "OPEN", "CLOSED", "DRAWN", "RESULT_PUBLISHED", "CANCELLED",
        name="ticket_series_status",
    )
    order_status = sa.Enum(
        "PENDING_PAYMENT", "PAYMENT_REVIEW", "PAID", "FULFILLED", "CANCELLED", "REFUND_PENDING", "REFUNDED",
        name="order_status",
    )
    delivery_status = sa.Enum("NOT_STARTED", "PENDING", "PROCESSING", "DELIVERED", "FAILED", name="delivery_status")
    product_type = sa.Enum("SERIES", "PACKAGE", name="ticket_product_type")
    reservation_status = sa.Enum("RESERVED", "ALLOCATED", "RELEASED", name="ticket_reservation_status")
    ticket_status = sa.Enum("ALLOCATED", "VOID", name="ticket_status")

    op.create_table(
        "ticket_series",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_paise", sa.BigInteger(), nullable=False),
        sa.Column("ticket_limit", sa.Integer(), nullable=False),
        sa.Column("sold_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("sales_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sales_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("draw_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", ticket_series_status, server_default="DRAFT", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("price_paise > 0", name="ck_ticket_series_price_positive"),
        sa.CheckConstraint("ticket_limit > 0", name="ck_ticket_series_limit_positive"),
        sa.CheckConstraint("sold_count >= 0", name="ck_ticket_series_sold_nonnegative"),
        sa.CheckConstraint("reserved_count >= 0", name="ck_ticket_series_reserved_nonnegative"),
        sa.CheckConstraint("sold_count + reserved_count <= ticket_limit", name="ck_ticket_series_inventory_within_limit"),
        sa.CheckConstraint("currency = 'INR'", name="ck_ticket_series_currency_inr"),
        sa.CheckConstraint("sales_end_at > sales_start_at", name="ck_ticket_series_sales_window"),
        sa.CheckConstraint("draw_at > sales_end_at", name="ck_ticket_series_draw_after_sales"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_series_created_by_user_id", "ticket_series", ["created_by_user_id"], unique=False)
    op.create_index("ix_ticket_series_status", "ticket_series", ["status"], unique=False)
    op.create_table(
        "ticket_packages",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_paise", sa.BigInteger(), nullable=False),
        sa.Column("inventory_limit", sa.Integer(), nullable=True),
        sa.Column("sold_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("price_paise > 0", name="ck_ticket_package_price_positive"),
        sa.CheckConstraint("inventory_limit IS NULL OR inventory_limit > 0", name="ck_ticket_package_limit_positive"),
        sa.CheckConstraint("sold_count >= 0", name="ck_ticket_package_sold_nonnegative"),
        sa.CheckConstraint("reserved_count >= 0", name="ck_ticket_package_reserved_nonnegative"),
        sa.CheckConstraint("inventory_limit IS NULL OR sold_count + reserved_count <= inventory_limit", name="ck_ticket_package_inventory_within_limit"),
        sa.CheckConstraint("currency = 'INR'", name="ck_ticket_package_currency_inr"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ticket_packages_created_by_user_id", "ticket_packages", ["created_by_user_id"], unique=False)
    op.create_table(
        "ticket_series_prizes",
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("prize_paise", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("rank > 0", name="ck_ticket_series_prize_rank_positive"),
        sa.CheckConstraint("prize_paise > 0", name="ck_ticket_series_prize_amount_positive"),
        sa.ForeignKeyConstraint(["series_id"], ["ticket_series.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "rank", name="uq_ticket_series_prize_rank"),
    )
    op.create_index("ix_ticket_series_prizes_series_id", "ticket_series_prizes", ["series_id"], unique=False)
    op.create_table(
        "ticket_package_items",
        sa.Column("package_id", sa.Uuid(), nullable=False),
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_ticket_package_item_quantity_positive"),
        sa.ForeignKeyConstraint(["package_id"], ["ticket_packages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["series_id"], ["ticket_series.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id", "series_id", name="uq_ticket_package_item_series"),
    )
    op.create_index("ix_ticket_package_items_package_id", "ticket_package_items", ["package_id"], unique=False)
    op.create_index("ix_ticket_package_items_series_id", "ticket_package_items", ["series_id"], unique=False)
    op.create_table(
        "orders",
        sa.Column("buyer_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", order_status, server_default="PENDING_PAYMENT", nullable=False),
        sa.Column("delivery_status", delivery_status, server_default="NOT_STARTED", nullable=False),
        sa.Column("total_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settlement_reference_id", sa.Uuid(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("total_paise > 0", name="ck_order_total_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_order_currency_inr"),
        sa.ForeignKeyConstraint(["buyer_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("settlement_reference_id"),
    )
    op.create_index("ix_orders_buyer_user_id", "orders", ["buyer_user_id"], unique=False)
    op.create_index("ix_orders_status", "orders", ["status"], unique=False)
    op.create_index("ix_orders_delivery_status", "orders", ["delivery_status"], unique=False)
    op.create_index("ix_orders_expires_at", "orders", ["expires_at"], unique=False)
    op.create_table(
        "order_items",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("product_type", product_type, nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("product_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("unit_price_paise", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_total_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("package_contents_snapshot", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("unit_price_paise > 0", name="ck_order_item_unit_price_positive"),
        sa.CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        sa.CheckConstraint("line_total_paise > 0", name="ck_order_item_total_positive"),
        sa.CheckConstraint("line_total_paise = unit_price_paise * quantity", name="ck_order_item_total_matches"),
        sa.CheckConstraint("currency = 'INR'", name="ck_order_item_currency_inr"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"], unique=False)
    op.create_index("ix_order_items_product_id", "order_items", ["product_id"], unique=False)
    op.create_table(
        "ticket_reservations",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("order_item_id", sa.Uuid(), nullable=False),
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", reservation_status, server_default="RESERVED", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_ticket_reservation_quantity_positive"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["series_id"], ["ticket_series.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", "series_id", name="uq_ticket_reservation_order_series"),
    )
    op.create_index("ix_ticket_reservations_order_id", "ticket_reservations", ["order_id"], unique=False)
    op.create_index("ix_ticket_reservations_order_item_id", "ticket_reservations", ["order_item_id"], unique=False)
    op.create_index("ix_ticket_reservations_series_id", "ticket_reservations", ["series_id"], unique=False)
    op.create_index("ix_ticket_reservations_status", "ticket_reservations", ["status"], unique=False)
    op.create_index("ix_ticket_reservations_expires_at", "ticket_reservations", ["expires_at"], unique=False)
    op.create_table(
        "tickets",
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("serial_number", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", ticket_status, server_default="ALLOCATED", nullable=False),
        sa.Column("is_winner", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("prize_paise", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("serial_number > 0", name="ck_ticket_serial_positive"),
        sa.CheckConstraint("prize_paise >= 0", name="ck_ticket_prize_nonnegative"),
        sa.ForeignKeyConstraint(["series_id"], ["ticket_series.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "serial_number", name="uq_ticket_series_serial"),
    )
    op.create_index("ix_tickets_series_id", "tickets", ["series_id"], unique=False)
    op.create_index("ix_tickets_order_item_id", "tickets", ["order_item_id"], unique=False)
    op.create_index("ix_tickets_owner_user_id", "tickets", ["owner_user_id"], unique=False)
    op.create_index("ix_tickets_status", "tickets", ["status"], unique=False)


def downgrade() -> None:
    for index_name, table_name in (
        ("ix_tickets_status", "tickets"),
        ("ix_tickets_owner_user_id", "tickets"),
        ("ix_tickets_order_item_id", "tickets"),
        ("ix_tickets_series_id", "tickets"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_table("tickets")
    for index_name in (
        "ix_ticket_reservations_expires_at", "ix_ticket_reservations_status",
        "ix_ticket_reservations_series_id", "ix_ticket_reservations_order_item_id",
        "ix_ticket_reservations_order_id",
    ):
        op.drop_index(index_name, table_name="ticket_reservations")
    op.drop_table("ticket_reservations")
    op.drop_index("ix_order_items_product_id", table_name="order_items")
    op.drop_index("ix_order_items_order_id", table_name="order_items")
    op.drop_table("order_items")
    for index_name in ("ix_orders_expires_at", "ix_orders_delivery_status", "ix_orders_status", "ix_orders_buyer_user_id"):
        op.drop_index(index_name, table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_ticket_package_items_series_id", table_name="ticket_package_items")
    op.drop_index("ix_ticket_package_items_package_id", table_name="ticket_package_items")
    op.drop_table("ticket_package_items")
    op.drop_index("ix_ticket_series_prizes_series_id", table_name="ticket_series_prizes")
    op.drop_table("ticket_series_prizes")
    op.drop_index("ix_ticket_packages_created_by_user_id", table_name="ticket_packages")
    op.drop_table("ticket_packages")
    op.drop_index("ix_ticket_series_status", table_name="ticket_series")
    op.drop_index("ix_ticket_series_created_by_user_id", table_name="ticket_series")
    op.drop_table("ticket_series")
    if op.get_bind().dialect.name == "postgresql":
        for enum_name in (
            "ticket_status", "ticket_reservation_status", "ticket_product_type",
            "delivery_status", "order_status", "ticket_series_status",
        ):
            op.execute(f"DROP TYPE {enum_name}")
