"""add Phase 7 coupon snapshots and settlement revenue allocations

Revision ID: 0012_phase7_coupon_revenue
Revises: 0011_phase6_draw_integrity_guards
Create Date: 2026-09-25
"""

from uuid import UUID

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite


revision = "0012_phase7_coupon_revenue"
down_revision = "0011_phase6_draw_integrity_guards"
branch_labels = None
depends_on = None


PLATFORM_REVENUE_ACCOUNTS: tuple[tuple[UUID, str, str], ...] = (
    (
        UUID("00000000-0000-0000-0000-000000000004"),
        "platform:revenue:prize-pool:inr",
        "PLATFORM_PRIZE_POOL",
    ),
    (
        UUID("00000000-0000-0000-0000-000000000005"),
        "platform:revenue:marketing:inr",
        "PLATFORM_MARKETING",
    ),
    (
        UUID("00000000-0000-0000-0000-000000000006"),
        "platform:revenue:operations:inr",
        "PLATFORM_OPERATIONS",
    ),
    (
        UUID("00000000-0000-0000-0000-000000000007"),
        "platform:revenue:emergency-reserve:inr",
        "PLATFORM_EMERGENCY_RESERVE",
    ),
    (
        UUID("00000000-0000-0000-0000-000000000008"),
        "platform:revenue:profit-growth:inr",
        "PLATFORM_PROFIT_GROWTH",
    ),
)

COUPON_DISCOUNT_TYPES = ("FIXED_PAISE", "PERCENT_BPS")
COUPON_REDEMPTION_STATUSES = ("RESERVED", "CONSUMED", "RELEASED")
REVENUE_ALLOCATION_SOURCES = ("EXTERNAL_ORDER_PENDING", "P2P_ORDER_PENDING")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        with op.get_context().autocommit_block():
            for kind in (value[2] for value in PLATFORM_REVENUE_ACCOUNTS):
                op.execute(f"ALTER TYPE ledger_account_kind ADD VALUE IF NOT EXISTS '{kind}'")
        _create_postgresql_enums()

    _seed_revenue_accounts()
    _add_order_price_snapshots(dialect)
    _create_coupon_tables()
    _create_revenue_allocation_table()
    if dialect == "postgresql":
        _install_postgresql_immutability_guard()


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _remove_postgresql_immutability_guard()
    op.drop_table("revenue_allocations")
    op.drop_table("coupon_redemptions")
    op.drop_table("coupons")
    _drop_order_price_snapshots(dialect)
    if dialect == "postgresql":
        for name, values in (
            ("revenue_allocation_source", REVENUE_ALLOCATION_SOURCES),
            ("coupon_redemption_status", COUPON_REDEMPTION_STATUSES),
            ("coupon_discount_type", COUPON_DISCOUNT_TYPES),
        ):
            postgresql.ENUM(*values, name=name).drop(op.get_bind(), checkfirst=True)
    # Platform revenue accounts and appended ledger-account enum members are
    # append-only accounting configuration, matching the earlier pending
    # account migrations. A later re-upgrade validates and reuses them.


def _column_enum(name: str, *values: str) -> sa.Enum:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name)


def _create_postgresql_enums() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*COUPON_DISCOUNT_TYPES, name="coupon_discount_type").create(bind, checkfirst=True)
    postgresql.ENUM(*COUPON_REDEMPTION_STATUSES, name="coupon_redemption_status").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(*REVENUE_ALLOCATION_SOURCES, name="revenue_allocation_source").create(
        bind, checkfirst=True
    )


def _seed_revenue_accounts() -> None:
    ledger_accounts = sa.table(
        "ledger_accounts",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("currency", sa.String()),
        sa.column("wallet_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
    )
    bind = op.get_bind()
    for account_id, code, kind in PLATFORM_REVENUE_ACCOUNTS:
        values = {
            "id": account_id,
            "code": code,
            "kind": kind,
            "currency": "INR",
            "wallet_id": None,
            "user_id": None,
        }
        if bind.dialect.name == "postgresql":
            kind_value = sa.cast(
                sa.literal(kind),
                postgresql.ENUM(name="ledger_account_kind", create_type=False),
            )
            statement = postgresql.insert(ledger_accounts).values(
                {**values, "kind": kind_value}
            ).on_conflict_do_nothing()
        elif bind.dialect.name == "sqlite":
            statement = sqlite.insert(ledger_accounts).values(values).on_conflict_do_nothing()
        else:
            raise RuntimeError("Phase 7 migration supports PostgreSQL and SQLite only")
        bind.execute(statement)

    if op.get_context().as_sql:
        return
    rows = bind.execute(
        sa.select(
            ledger_accounts.c.id,
            ledger_accounts.c.code,
            ledger_accounts.c.kind,
            ledger_accounts.c.currency,
            ledger_accounts.c.wallet_id,
            ledger_accounts.c.user_id,
        ).where(
            sa.or_(
                ledger_accounts.c.id.in_([value[0] for value in PLATFORM_REVENUE_ACCOUNTS]),
                ledger_accounts.c.code.in_([value[1] for value in PLATFORM_REVENUE_ACCOUNTS]),
            )
        )
    ).mappings().all()
    expected = {(str(account_id), code, kind) for account_id, code, kind in PLATFORM_REVENUE_ACCOUNTS}
    actual = {
        (str(row["id"]), row["code"], row["kind"])
        for row in rows
        if row["currency"] == "INR" and row["wallet_id"] is None and row["user_id"] is None
    }
    if actual != expected:
        raise RuntimeError("Phase 7 revenue ledger account configuration conflicts with immutable values")


def _add_order_price_snapshots(dialect: str) -> None:
    op.add_column(
        "orders",
        sa.Column("subtotal_paise", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "orders",
        sa.Column("discount_paise", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("orders", sa.Column("coupon_code_snapshot", sa.String(length=64), nullable=True))
    op.execute("UPDATE orders SET subtotal_paise = total_paise, discount_paise = 0")
    constraints = (
        ("ck_order_subtotal_positive", "subtotal_paise > 0"),
        ("ck_order_discount_nonnegative", "discount_paise >= 0"),
        (
            "ck_order_amount_snapshot_matches",
            "subtotal_paise = total_paise + discount_paise",
        ),
        (
            "ck_order_coupon_code_uppercase",
            "coupon_code_snapshot IS NULL OR coupon_code_snapshot = upper(coupon_code_snapshot)",
        ),
    )
    if dialect == "sqlite":
        with op.batch_alter_table("orders", recreate="always") as batch:
            for name, condition in constraints:
                batch.create_check_constraint(name, condition)
    else:
        for name, condition in constraints:
            op.create_check_constraint(name, "orders", condition)


def _drop_order_price_snapshots(dialect: str) -> None:
    constraints = (
        "ck_order_coupon_code_uppercase",
        "ck_order_amount_snapshot_matches",
        "ck_order_discount_nonnegative",
        "ck_order_subtotal_positive",
    )
    if dialect == "sqlite":
        with op.batch_alter_table("orders", recreate="always") as batch:
            for name in constraints:
                batch.drop_constraint(name, type_="check")
            batch.drop_column("coupon_code_snapshot")
            batch.drop_column("discount_paise")
            batch.drop_column("subtotal_paise")
    else:
        for name in constraints:
            op.drop_constraint(name, "orders", type_="check")
        op.drop_column("orders", "coupon_code_snapshot")
        op.drop_column("orders", "discount_paise")
        op.drop_column("orders", "subtotal_paise")


def _create_coupon_tables() -> None:
    discount_type = _column_enum("coupon_discount_type", *COUPON_DISCOUNT_TYPES)
    redemption_status = _column_enum("coupon_redemption_status", *COUPON_REDEMPTION_STATUSES)
    op.create_table(
        "coupons",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("discount_type", discount_type, nullable=False),
        sa.Column("fixed_discount_paise", sa.BigInteger(), nullable=True),
        sa.Column("percentage_bps", sa.Integer(), nullable=True),
        sa.Column("max_discount_paise", sa.BigInteger(), nullable=True),
        sa.Column("minimum_order_paise", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("usage_limit", sa.Integer(), nullable=True),
        sa.Column("per_user_limit", sa.Integer(), nullable=True),
        sa.Column("active_redemption_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("code = upper(code)", name="ck_coupon_code_uppercase"),
        sa.CheckConstraint("minimum_order_paise >= 0", name="ck_coupon_minimum_nonnegative"),
        sa.CheckConstraint("active_redemption_count >= 0", name="ck_coupon_active_count_nonnegative"),
        sa.CheckConstraint("usage_limit IS NULL OR usage_limit > 0", name="ck_coupon_usage_limit_positive"),
        sa.CheckConstraint(
            "per_user_limit IS NULL OR per_user_limit > 0", name="ck_coupon_per_user_limit_positive"
        ),
        sa.CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at",
            name="ck_coupon_validity_window",
        ),
        sa.CheckConstraint(
            "("
            "discount_type = 'FIXED_PAISE' "
            "AND fixed_discount_paise IS NOT NULL AND fixed_discount_paise > 0 "
            "AND percentage_bps IS NULL AND max_discount_paise IS NULL"
            ") OR ("
            "discount_type = 'PERCENT_BPS' "
            "AND percentage_bps IS NOT NULL AND percentage_bps > 0 AND percentage_bps <= 10000 "
            "AND fixed_discount_paise IS NULL "
            "AND (max_discount_paise IS NULL OR max_discount_paise > 0)"
            ")",
            name="ck_coupon_discount_definition",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_coupons_code"),
    )
    for name, columns in (
        ("ix_coupons_code", ["code"]),
        ("ix_coupons_starts_at", ["starts_at"]),
        ("ix_coupons_ends_at", ["ends_at"]),
        ("ix_coupons_is_active", ["is_active"]),
        ("ix_coupons_created_by_user_id", ["created_by_user_id"]),
    ):
        op.create_index(name, "coupons", columns, unique=False)

    op.create_table(
        "coupon_redemptions",
        sa.Column("coupon_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("buyer_user_id", sa.Uuid(), nullable=False),
        sa.Column("coupon_code_snapshot", sa.String(length=64), nullable=False),
        sa.Column("rule_snapshot", sa.JSON(), nullable=False),
        sa.Column("gross_paise", sa.BigInteger(), nullable=False),
        sa.Column("discount_paise", sa.BigInteger(), nullable=False),
        sa.Column("net_paise", sa.BigInteger(), nullable=False),
        sa.Column("status", redemption_status, server_default="RESERVED", nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("gross_paise > 0", name="ck_coupon_redemption_gross_positive"),
        sa.CheckConstraint("discount_paise > 0", name="ck_coupon_redemption_discount_positive"),
        sa.CheckConstraint("net_paise > 0", name="ck_coupon_redemption_net_positive"),
        sa.CheckConstraint(
            "gross_paise = discount_paise + net_paise",
            name="ck_coupon_redemption_amounts_match",
        ),
        sa.CheckConstraint(
            "("
            "status = 'RESERVED' AND consumed_at IS NULL AND released_at IS NULL"
            ") OR ("
            "status = 'CONSUMED' AND consumed_at IS NOT NULL AND released_at IS NULL"
            ") OR ("
            "status = 'RELEASED' AND released_at IS NOT NULL AND consumed_at IS NULL"
            ")",
            name="ck_coupon_redemption_status_timestamps",
        ),
        sa.ForeignKeyConstraint(["coupon_id"], ["coupons.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["buyer_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_coupon_redemption_order"),
    )
    for name, columns in (
        ("ix_coupon_redemptions_coupon_id", ["coupon_id"]),
        ("ix_coupon_redemptions_order_id", ["order_id"]),
        ("ix_coupon_redemptions_buyer_user_id", ["buyer_user_id"]),
        ("ix_coupon_redemptions_status", ["status"]),
    ):
        op.create_index(name, "coupon_redemptions", columns, unique=False)


def _create_revenue_allocation_table() -> None:
    source = _column_enum("revenue_allocation_source", *REVENUE_ALLOCATION_SOURCES)
    op.create_table(
        "revenue_allocations",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("settlement_reference_id", sa.Uuid(), nullable=False),
        sa.Column("source", source, nullable=False),
        sa.Column("source_account_id", sa.Uuid(), nullable=False),
        sa.Column("journal_group_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("allocation_base_paise", sa.BigInteger(), nullable=False),
        sa.Column("prize_pool_paise", sa.BigInteger(), nullable=False),
        sa.Column("marketing_paise", sa.BigInteger(), nullable=False),
        sa.Column("operations_paise", sa.BigInteger(), nullable=False),
        sa.Column("reserve_paise", sa.BigInteger(), nullable=False),
        sa.Column("profit_growth_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("allocated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
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
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_account_id"], ["ledger_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["journal_group_id"], ["journal_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["idempotency_record_id"], ["idempotency_records.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["allocated_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_revenue_allocation_order"),
        sa.UniqueConstraint("settlement_reference_id", name="uq_revenue_allocation_settlement"),
        sa.UniqueConstraint("journal_group_id", name="uq_revenue_allocation_journal_group"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_revenue_allocation_idempotency"),
    )
    for name, columns in (
        ("ix_revenue_allocations_order_id", ["order_id"]),
        ("ix_revenue_allocations_settlement_reference_id", ["settlement_reference_id"]),
        ("ix_revenue_allocations_source", ["source"]),
        ("ix_revenue_allocations_allocated_at", ["allocated_at"]),
    ):
        op.create_index(name, "revenue_allocations", columns, unique=False)


def _install_postgresql_immutability_guard() -> None:
    op.execute(
        """
        CREATE FUNCTION prohibit_revenue_allocation_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'revenue allocations are append-only' USING ERRCODE = '55000';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER revenue_allocations_append_only
        BEFORE UPDATE OR DELETE ON revenue_allocations
        FOR EACH ROW EXECUTE FUNCTION prohibit_revenue_allocation_mutation();
        """
    )


def _remove_postgresql_immutability_guard() -> None:
    op.execute("DROP TRIGGER IF EXISTS revenue_allocations_append_only ON revenue_allocations")
    op.execute("DROP FUNCTION IF EXISTS prohibit_revenue_allocation_mutation()")
