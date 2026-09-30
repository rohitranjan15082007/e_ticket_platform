"""Alembic migration round-trip coverage through Phase 7 financial records."""

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.config import get_settings
from app.models.ledger import (
    PLATFORM_CLEARING_ACCOUNT_CODE,
    PLATFORM_EMERGENCY_RESERVE_ACCOUNT_CODE,
    PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE,
    PLATFORM_MARKETING_ACCOUNT_CODE,
    PLATFORM_OPERATIONS_ACCOUNT_CODE,
    PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE,
    PLATFORM_PRIZE_POOL_ACCOUNT_CODE,
    PLATFORM_PROFIT_GROWTH_ACCOUNT_CODE,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PHASE4_REVISION = "0007_p2p_provider_events_outbox"
PHASE5_REVISION = "0009_phase5_manual_destination_uniqueness"
PHASE6_REVISION = "0011_phase6_draw_integrity_guards"
PHASE7_REVISION = "0013_phase7_marketing_rewards"
FOUNDATION_TABLES = {
    "alembic_version",
    "idempotency_records",
    "roles",
    "user_roles",
    "users",
}
FINANCIAL_TABLES = {
    "audit_logs",
    "journal_groups",
    "journal_postings",
    "ledger_accounts",
    "wallet_holds",
    "wallets",
}
PHASE3_TABLES = {
    "ticket_series",
    "ticket_series_prizes",
    "ticket_packages",
    "ticket_package_items",
    "orders",
    "order_items",
    "ticket_reservations",
    "tickets",
}
PHASE4_TABLES = {
    "payment_destinations",
    "withdrawal_requests",
    "p2p_matches",
    "p2p_payment_submissions",
    "p2p_verified_payment_references",
    "p2p_receiver_confirmations",
    "p2p_disputes",
    "p2p_admin_resolutions",
    "p2p_settlements",
    "p2p_refunds",
    "p2p_outbox_events",
    "p2p_provider_events",
    "p2p_notifications",
}
PHASE5_TABLES = {
    "manual_payment_destinations",
    "payment_attempts",
    "payment_provider_events",
    "manual_payment_proofs",
    "payment_reconciliation_runs",
    "payment_settlements",
    "payment_outbox_events",
}
PHASE6_TABLES = {
    "draws",
    "draw_entries",
    "winners",
    "prize_awards",
    "draw_notifications",
}
PHASE7_TABLES = {
    "coupons",
    "coupon_redemptions",
    "revenue_allocations",
    "referral_profiles",
    "referral_programs",
    "referrals",
    "referral_rewards",
    "cashback_campaigns",
    "cashback_rewards",
    "affiliates",
    "affiliate_clicks",
    "affiliate_conversions",
    "affiliate_commissions",
}


def _config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _offline_config(database_url: str, output_buffer: StringIO) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=output_buffer)
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _table_names(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _column_names(database_url: str, table_name: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table_name)}
    finally:
        engine.dispose()


def _index_names(database_url: str, table_name: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return {index["name"] for index in inspect(engine).get_indexes(table_name)}
    finally:
        engine.dispose()


def _revision(database_url: str) -> str:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return str(connection.scalar(text("SELECT version_num FROM alembic_version")))
    finally:
        engine.dispose()


def test_sqlite_migrations_upgrade_downgrade_and_reupgrade(monkeypatch, tmp_path) -> None:
    """Exercise the real migration chain, including the hold lifecycle upgrade."""

    database_path = tmp_path / "phase2_migrations.db"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    sync_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("TICKET_DATABASE_URL", async_url)
    get_settings.cache_clear()
    config = _config(async_url)
    try:
        command.upgrade(config, "0001_foundation_identity")
        assert FOUNDATION_TABLES <= _table_names(sync_url)
        assert not (FINANCIAL_TABLES & _table_names(sync_url))
        assert _revision(sync_url) == "0001_foundation_identity"

        command.upgrade(config, PHASE7_REVISION)
        assert (
            FOUNDATION_TABLES
            | FINANCIAL_TABLES
            | PHASE3_TABLES
            | PHASE4_TABLES
            | PHASE5_TABLES
            | PHASE6_TABLES
            | PHASE7_TABLES
            <= _table_names(sync_url)
        )
        assert _revision(sync_url) == PHASE7_REVISION
        assert "response_payload" in _column_names(sync_url, "idempotency_records")
        assert {
            "wallet_id", "hold_journal_group_id", "release_journal_group_id", "settlement_journal_group_id"
        } <= _column_names(
            sync_url, "wallet_holds"
        )
        assert {
            "verified_payment_reference_id",
            "liable_party",
            "executor_reference",
            "created_by_user_id",
            "payout_verified_by_user_id",
            "payout_verified_at",
        } <= _column_names(sync_url, "p2p_refunds")
        assert {
            "provider_namespace",
            "external_event_id",
            "payload_digest",
            "status",
        } <= _column_names(sync_url, "p2p_provider_events")
        assert {"outbox_event_id", "user_id", "event_type", "delivered_at"} <= _column_names(
            sync_url, "p2p_notifications"
        )
        assert {
            "method",
            "provider_namespace",
            "provider_amount",
            "provider_currency",
            "telegram_invoice_payload",
            "telegram_payment_charge_id",
            "manual_destination_id",
            "idempotency_record_id",
        } <= _column_names(sync_url, "payment_attempts")
        assert {
            "provider_namespace",
            "external_event_id",
            "payload_digest",
            "telegram_payment_charge_id",
            "status",
        } <= _column_names(sync_url, "payment_provider_events")
        assert {
            "payment_attempt_id",
            "manual_destination_id",
            "utr",
            "submitted_amount_paise",
            "status",
        } <= _column_names(sync_url, "manual_payment_proofs")
        assert {
            "payment_attempt_id",
            "order_id",
            "journal_group_id",
            "manual_payment_proof_id",
            "verification_source",
        } <= _column_names(sync_url, "payment_settlements")
        assert "uq_manual_payment_destination_one_active" in _index_names(
            sync_url, "manual_payment_destinations"
        )
        assert {
            "series_id",
            "status",
            "seed_commitment",
            "eligible_tickets_digest",
            "prize_snapshot",
            "result_digest",
        } <= _column_names(sync_url, "draws")
        assert {"draw_id", "ticket_id", "serial_number", "owner_user_id"} <= _column_names(
            sync_url, "draw_entries"
        )
        assert {"draw_id", "ticket_id", "rank", "selection_digest"} <= _column_names(sync_url, "winners")
        assert {"winner_id", "wallet_id", "journal_group_id", "idempotency_record_id"} <= _column_names(
            sync_url, "prize_awards"
        )
        assert {"subtotal_paise", "discount_paise", "coupon_code_snapshot"} <= _column_names(
            sync_url, "orders"
        )
        assert {
            "code",
            "discount_type",
            "active_redemption_count",
            "usage_limit",
            "per_user_limit",
        } <= _column_names(sync_url, "coupons")
        assert {
            "coupon_id",
            "order_id",
            "rule_snapshot",
            "gross_paise",
            "discount_paise",
            "net_paise",
            "status",
        } <= _column_names(sync_url, "coupon_redemptions")
        assert {
            "order_id",
            "settlement_reference_id",
            "source",
            "source_account_id",
            "journal_group_id",
            "allocation_base_paise",
            "profit_growth_paise",
        } <= _column_names(sync_url, "revenue_allocations")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as connection:
                seeded_clearing_accounts = connection.scalar(
                    text("SELECT COUNT(*) FROM ledger_accounts WHERE code = :code"),
                    {"code": PLATFORM_CLEARING_ACCOUNT_CODE},
                )
                seeded_pending_accounts = connection.scalar(
                    text("SELECT COUNT(*) FROM ledger_accounts WHERE code = :code"),
                    {"code": PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE},
                )
                seeded_external_pending_accounts = connection.scalar(
                    text("SELECT COUNT(*) FROM ledger_accounts WHERE code = :code"),
                    {"code": PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE},
                )
                seeded_revenue_accounts = connection.scalar(
                    text("SELECT COUNT(*) FROM ledger_accounts WHERE code IN (:prize, :marketing, :operations, :reserve, :profit)"),
                    {
                        "prize": PLATFORM_PRIZE_POOL_ACCOUNT_CODE,
                        "marketing": PLATFORM_MARKETING_ACCOUNT_CODE,
                        "operations": PLATFORM_OPERATIONS_ACCOUNT_CODE,
                        "reserve": PLATFORM_EMERGENCY_RESERVE_ACCOUNT_CODE,
                        "profit": PLATFORM_PROFIT_GROWTH_ACCOUNT_CODE,
                    },
                )
        finally:
            engine.dispose()
        assert seeded_clearing_accounts == 1
        assert seeded_pending_accounts == 1
        assert seeded_external_pending_accounts == 1
        assert seeded_revenue_accounts == 5

        # The Phase 5 migrations leave their append-only account behind when the payment tables
        # are downgraded. Re-upgrade must reuse the immutable account and
        # recreate only the Phase 5 tables.
        command.downgrade(config, PHASE4_REVISION)
        assert FOUNDATION_TABLES | FINANCIAL_TABLES | PHASE3_TABLES | PHASE4_TABLES <= _table_names(sync_url)
        assert not ((PHASE5_TABLES | PHASE6_TABLES | PHASE7_TABLES) & _table_names(sync_url))
        assert _revision(sync_url) == PHASE4_REVISION

        engine = create_engine(sync_url)
        try:
            with engine.connect() as connection:
                external_pending_after_phase5_downgrade = connection.scalar(
                    text("SELECT COUNT(*) FROM ledger_accounts WHERE code = :code"),
                    {"code": PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE},
                )
        finally:
            engine.dispose()
        assert external_pending_after_phase5_downgrade == 1

        command.upgrade(config, PHASE7_REVISION)
        assert (
            FOUNDATION_TABLES
            | FINANCIAL_TABLES
            | PHASE3_TABLES
            | PHASE4_TABLES
            | PHASE5_TABLES
            | PHASE6_TABLES
            | PHASE7_TABLES
            <= _table_names(sync_url)
        )
        assert _revision(sync_url) == PHASE7_REVISION

        # 0005 intentionally leaves its append-only platform account behind
        # on downgrade.  Re-applying 0005 via the 0006 -> 0004 -> 0006 path
        # must reuse that exact account instead of failing with a duplicate
        # primary-key/code insert.
        command.downgrade(config, "0004_ticket_catalog_orders")
        assert FOUNDATION_TABLES | FINANCIAL_TABLES | PHASE3_TABLES <= _table_names(sync_url)
        assert not ((PHASE4_TABLES | PHASE5_TABLES | PHASE6_TABLES | PHASE7_TABLES) & _table_names(sync_url))
        assert _revision(sync_url) == "0004_ticket_catalog_orders"

        engine = create_engine(sync_url)
        try:
            with engine.connect() as connection:
                pending_accounts_after_phase4_downgrade = connection.scalar(
                    text("SELECT COUNT(*) FROM ledger_accounts WHERE code = :code"),
                    {"code": PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE},
                )
        finally:
            engine.dispose()
        assert pending_accounts_after_phase4_downgrade == 1

        command.upgrade(config, PHASE7_REVISION)
        assert (
            FOUNDATION_TABLES
            | FINANCIAL_TABLES
            | PHASE3_TABLES
            | PHASE4_TABLES
            | PHASE5_TABLES
            | PHASE6_TABLES
            | PHASE7_TABLES
            <= _table_names(sync_url)
        )
        assert _revision(sync_url) == PHASE7_REVISION

        engine = create_engine(sync_url)
        try:
            with engine.connect() as connection:
                pending_accounts_after_phase4_reupgrade = connection.scalar(
                    text("SELECT COUNT(*) FROM ledger_accounts WHERE code = :code"),
                    {"code": PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE},
                )
                external_pending_after_reupgrade = connection.scalar(
                    text("SELECT COUNT(*) FROM ledger_accounts WHERE code = :code"),
                    {"code": PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE},
                )
        finally:
            engine.dispose()
        assert pending_accounts_after_phase4_reupgrade == 1
        assert external_pending_after_reupgrade == 1

        command.downgrade(config, "0001_foundation_identity")
        assert FOUNDATION_TABLES <= _table_names(sync_url)
        assert not (
            (FINANCIAL_TABLES | PHASE3_TABLES | PHASE4_TABLES | PHASE5_TABLES | PHASE6_TABLES | PHASE7_TABLES)
            & _table_names(sync_url)
        )
        assert "response_payload" not in _column_names(sync_url, "idempotency_records")
        assert _revision(sync_url) == "0001_foundation_identity"

        command.upgrade(config, PHASE7_REVISION)
        assert (
            FOUNDATION_TABLES
            | FINANCIAL_TABLES
            | PHASE3_TABLES
            | PHASE4_TABLES
            | PHASE5_TABLES
            | PHASE6_TABLES
            | PHASE7_TABLES
            <= _table_names(sync_url)
        )
        assert _revision(sync_url) == PHASE7_REVISION
    finally:
        get_settings.cache_clear()


def test_postgresql_offline_sql_renders_phase7_integrity_guards(monkeypatch) -> None:
    """Exercise every revision's PostgreSQL offline branch without a live server."""

    database_url = "postgresql+asyncpg://ticket_user:ticket_password@localhost/ticket_platform"
    monkeypatch.setenv("TICKET_DATABASE_URL", database_url)
    get_settings.cache_clear()
    output_buffer = StringIO()
    config = _offline_config(database_url, output_buffer)
    try:
        command.upgrade(config, PHASE7_REVISION, sql=True)
    finally:
        get_settings.cache_clear()

    rendered = output_buffer.getvalue()
    assert "CREATE FUNCTION enforce_draw_transition()" in rendered
    assert "CREATE FUNCTION ensure_series_open_has_draw_commitment()" in rendered
    assert "CREATE TRIGGER draws_controlled_transition" in rendered
    assert "CREATE TRIGGER ticket_series_open_requires_draw_commitment" in rendered
    assert "fk_winner_draw_entry" in rendered
    assert "fk_prize_award_draw_winner" in rendered
    assert "ALTER TYPE ledger_account_kind ADD VALUE IF NOT EXISTS 'PLATFORM_PRIZE_POOL'" in rendered
    assert "CREATE TABLE coupons" in rendered
    assert "CREATE TABLE coupon_redemptions" in rendered
    assert "CREATE TABLE revenue_allocations" in rendered
    assert "CREATE TRIGGER revenue_allocations_append_only" in rendered
    assert "CREATE TABLE referral_programs" in rendered
    assert "CREATE TABLE referral_rewards" in rendered
    assert "CREATE TABLE cashback_campaigns" in rendered
    assert "CREATE TABLE cashback_rewards" in rendered
    assert "CREATE TABLE affiliates" in rendered
    assert "CREATE TABLE affiliate_conversions" in rendered
    assert "CREATE TABLE affiliate_commissions" in rendered
