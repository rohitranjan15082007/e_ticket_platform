"""add generic external payment persistence and ledger configuration

Revision ID: 0008_phase5_payment_core
Revises: 0007_p2p_provider_events_outbox
Create Date: 2026-09-23
"""

from uuid import UUID

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite


revision = "0008_phase5_payment_core"
down_revision = "0007_p2p_provider_events_outbox"
branch_labels = None
depends_on = None


PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000003")
PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE = "platform:external-order-pending:inr"
ACTIVE_ATTEMPTS = "status IN ('AWAITING_PAYMENT', 'UNDER_REVIEW')"

ENUMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("payment_method", ("WHITE_LABEL", "TELEGRAM_STARS", "MANUAL_UPI")),
    (
        "payment_attempt_status",
        ("AWAITING_PAYMENT", "UNDER_REVIEW", "SUCCEEDED", "FAILED", "EXPIRED", "CANCELLED"),
    ),
    (
        "payment_provider_event_status",
        ("RECEIVED", "PROCESSING", "PROCESSED", "REVIEW_REQUIRED", "REJECTED", "FAILED"),
    ),
    ("payment_outbox_event_status", ("PENDING", "PROCESSING", "DELIVERED", "FAILED")),
    (
        "payment_reconciliation_status",
        ("PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"),
    ),
    ("manual_payment_destination_status", ("PENDING_APPROVAL", "ACTIVE", "DISABLED")),
    (
        "manual_payment_proof_status",
        ("SUBMITTED", "UNDER_REVIEW", "APPROVED", "REJECTED", "CANCELLED"),
    ),
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        # An enum member cannot be used safely in the same transaction in
        # which PostgreSQL added it. Commit the account-kind extension first.
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE ledger_account_kind ADD VALUE IF NOT EXISTS "
                "'PLATFORM_EXTERNAL_ORDER_PENDING'"
            )
        _create_postgresql_enums()

    _seed_external_order_pending_account()
    _create_manual_destinations()
    _create_payment_attempts()
    _create_provider_events()
    _create_manual_proofs()
    _create_reconciliation_runs()
    _create_settlements()
    _create_outbox_events()


def downgrade() -> None:
    _drop_outbox_events()
    _drop_settlements()
    _drop_reconciliation_runs()
    _drop_manual_proofs()
    _drop_provider_events()
    _drop_payment_attempts()
    _drop_manual_destinations()
    if op.get_bind().dialect.name == "postgresql":
        for name, values in reversed(ENUMS):
            postgresql.ENUM(*values, name=name).drop(op.get_bind(), checkfirst=True)
    # The append-only ledger account and PostgreSQL enum member intentionally
    # remain. Re-upgrade must reuse, rather than rewrite, accounting config.


def _column_enum(name: str, *values: str) -> sa.Enum:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name)


def _create_postgresql_enums() -> None:
    bind = op.get_bind()
    for name, values in ENUMS:
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)


def _seed_external_order_pending_account() -> None:
    ledger_accounts = sa.table(
        "ledger_accounts",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("currency", sa.String()),
        sa.column("wallet_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
    )
    values = {
        "id": PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_ID,
        "code": PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE,
        "kind": "PLATFORM_EXTERNAL_ORDER_PENDING",
        "currency": "INR",
        "wallet_id": None,
        "user_id": None,
    }
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        kind = sa.cast(
            sa.literal(values["kind"]),
            postgresql.ENUM(name="ledger_account_kind", create_type=False),
        )
        statement = postgresql.insert(ledger_accounts).values(
            {**values, "kind": kind}
        ).on_conflict_do_nothing()
    elif bind.dialect.name == "sqlite":
        statement = sqlite.insert(ledger_accounts).values(values).on_conflict_do_nothing()
    else:
        raise RuntimeError("Phase 5 payment-core migration supports PostgreSQL and SQLite only")
    bind.execute(statement)

    # The account survives downgrade, so a re-upgrade must prove that an
    # existing row is the exact immutable canonical configuration.
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
                ledger_accounts.c.id == PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_ID,
                ledger_accounts.c.code == PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE,
            )
        )
    ).mappings().all()
    if len(rows) != 1:
        raise RuntimeError("External order-pending ledger account identity is not unique")
    account = rows[0]
    if (
        str(account["id"]) != str(PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_ID)
        or account["code"] != PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE
        or account["kind"] != "PLATFORM_EXTERNAL_ORDER_PENDING"
        or account["currency"] != "INR"
        or account["wallet_id"] is not None
        or account["user_id"] is not None
    ):
        raise RuntimeError("External order-pending ledger account conflicts with immutable configuration")


def _create_manual_destinations() -> None:
    destination_status = _column_enum(
        "manual_payment_destination_status", "PENDING_APPROVAL", "ACTIVE", "DISABLED"
    )
    op.create_table(
        "manual_payment_destinations",
        sa.Column("display_label", sa.String(length=120), nullable=False),
        sa.Column("upi_id", sa.String(length=255), nullable=False),
        sa.Column("qr_reference", sa.String(length=255), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("status", destination_status, server_default="PENDING_APPROVAL", nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approval_evidence_reference", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "(status <> 'ACTIVE') OR (approved_by_user_id IS NOT NULL "
            "AND approval_evidence_reference IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_manual_payment_destination_active_approved",
        ),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_manual_payment_destinations_status", "manual_payment_destinations", ["status"], unique=False
    )


def _create_payment_attempts() -> None:
    method = _column_enum("payment_method", "WHITE_LABEL", "TELEGRAM_STARS", "MANUAL_UPI")
    attempt_status = _column_enum(
        "payment_attempt_status", "AWAITING_PAYMENT", "UNDER_REVIEW", "SUCCEEDED", "FAILED", "EXPIRED", "CANCELLED"
    )
    op.create_table(
        "payment_attempts",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("buyer_user_id", sa.Uuid(), nullable=False),
        sa.Column("method", method, nullable=False),
        sa.Column("provider_namespace", sa.String(length=64), nullable=False),
        sa.Column("status", attempt_status, server_default="AWAITING_PAYMENT", nullable=False),
        sa.Column("order_amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("order_currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("provider_amount", sa.BigInteger(), nullable=False),
        sa.Column("provider_currency", sa.String(length=3), nullable=False),
        sa.Column("method_data_snapshot", sa.JSON(), nullable=False),
        sa.Column("merchant_reference", sa.String(length=160), nullable=False),
        sa.Column("provider_order_reference", sa.String(length=160), nullable=True),
        sa.Column("provider_payment_reference", sa.String(length=160), nullable=True),
        sa.Column("telegram_invoice_payload", sa.String(length=255), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_payment_charge_id", sa.String(length=255), nullable=True),
        sa.Column("manual_destination_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("order_amount_paise > 0", name="ck_payment_attempt_order_amount_positive"),
        sa.CheckConstraint("order_currency = 'INR'", name="ck_payment_attempt_order_currency_inr"),
        sa.CheckConstraint("provider_amount > 0", name="ck_payment_attempt_provider_amount_positive"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["buyer_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["manual_destination_id"], ["manual_payment_destinations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["idempotency_record_id"], ["idempotency_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_payment_attempt_idempotency"),
        sa.UniqueConstraint("merchant_reference", name="uq_payment_attempt_merchant_reference"),
        sa.UniqueConstraint(
            "provider_namespace", "provider_payment_reference", name="uq_payment_attempt_provider_payment_reference"
        ),
        sa.UniqueConstraint("telegram_invoice_payload", name="uq_payment_attempt_telegram_invoice_payload"),
        sa.UniqueConstraint("telegram_payment_charge_id", name="uq_payment_attempt_telegram_charge"),
    )
    for name, columns in (
        ("ix_payment_attempts_order_id", ["order_id"]),
        ("ix_payment_attempts_buyer_user_id", ["buyer_user_id"]),
        ("ix_payment_attempts_method", ["method"]),
        ("ix_payment_attempts_provider_namespace", ["provider_namespace"]),
        ("ix_payment_attempts_status", ["status"]),
        ("ix_payment_attempts_manual_destination_id", ["manual_destination_id"]),
        ("ix_payment_attempts_expires_at", ["expires_at"]),
    ):
        op.create_index(name, "payment_attempts", columns, unique=False)
    op.create_index(
        "uq_payment_attempt_one_active_order",
        "payment_attempts",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_ATTEMPTS),
        sqlite_where=sa.text(ACTIVE_ATTEMPTS),
    )


def _create_provider_events() -> None:
    event_status = _column_enum(
        "payment_provider_event_status", "RECEIVED", "PROCESSING", "PROCESSED", "REVIEW_REQUIRED", "REJECTED", "FAILED"
    )
    op.create_table(
        "payment_provider_events",
        sa.Column("provider_namespace", sa.String(length=64), nullable=False),
        sa.Column("external_event_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("request_headers", sa.JSON(), nullable=True),
        sa.Column("payment_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("merchant_reference", sa.String(length=160), nullable=True),
        sa.Column("provider_payment_reference", sa.String(length=160), nullable=True),
        sa.Column("provider_amount", sa.BigInteger(), nullable=True),
        sa.Column("provider_currency", sa.String(length=3), nullable=True),
        sa.Column("telegram_payment_charge_id", sa.String(length=255), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signature_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", event_status, server_default="RECEIVED", nullable=False),
        sa.Column("processing_error", sa.String(length=500), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "provider_amount IS NULL OR provider_amount > 0", name="ck_payment_provider_event_amount_positive"
        ),
        sa.ForeignKeyConstraint(["payment_attempt_id"], ["payment_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_namespace", "external_event_id", name="uq_payment_provider_event_external"),
        sa.UniqueConstraint("telegram_payment_charge_id", name="uq_payment_provider_event_telegram_charge"),
    )
    for name, columns in (
        ("ix_payment_provider_events_payment_attempt_id", ["payment_attempt_id"]),
        ("ix_payment_provider_events_merchant_reference", ["merchant_reference"]),
        ("ix_payment_provider_events_provider_payment_reference", ["provider_payment_reference"]),
        ("ix_payment_provider_events_status", ["status"]),
    ):
        op.create_index(name, "payment_provider_events", columns, unique=False)


def _create_manual_proofs() -> None:
    proof_status = _column_enum(
        "manual_payment_proof_status", "SUBMITTED", "UNDER_REVIEW", "APPROVED", "REJECTED", "CANCELLED"
    )
    op.create_table(
        "manual_payment_proofs",
        sa.Column("payment_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("manual_destination_id", sa.Uuid(), nullable=False),
        sa.Column("buyer_user_id", sa.Uuid(), nullable=False),
        sa.Column("utr", sa.String(length=160), nullable=False),
        sa.Column("proof_reference", sa.String(length=255), nullable=False),
        sa.Column("proof_metadata", sa.JSON(), nullable=True),
        sa.Column("submitted_amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("submitted_currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", proof_status, server_default="SUBMITTED", nullable=False),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("submitted_amount_paise > 0", name="ck_manual_payment_proof_amount_positive"),
        sa.CheckConstraint("submitted_currency = 'INR'", name="ck_manual_payment_proof_currency_inr"),
        sa.CheckConstraint(
            "(status NOT IN ('APPROVED', 'REJECTED')) OR "
            "(reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL AND review_note IS NOT NULL)",
            name="ck_manual_payment_proof_terminal_review",
        ),
        sa.ForeignKeyConstraint(["payment_attempt_id"], ["payment_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["manual_destination_id"], ["manual_payment_destinations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["buyer_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("utr", name="uq_manual_payment_proof_utr"),
    )
    for name, columns in (
        ("ix_manual_payment_proofs_payment_attempt_id", ["payment_attempt_id"]),
        ("ix_manual_payment_proofs_manual_destination_id", ["manual_destination_id"]),
        ("ix_manual_payment_proofs_buyer_user_id", ["buyer_user_id"]),
        ("ix_manual_payment_proofs_status", ["status"]),
    ):
        op.create_index(name, "manual_payment_proofs", columns, unique=False)


def _create_reconciliation_runs() -> None:
    method = _column_enum("payment_method", "WHITE_LABEL", "TELEGRAM_STARS", "MANUAL_UPI")
    reconciliation_status = _column_enum(
        "payment_reconciliation_status", "PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"
    )
    op.create_table(
        "payment_reconciliation_runs",
        sa.Column("run_key", sa.String(length=255), nullable=False),
        sa.Column("provider_namespace", sa.String(length=64), nullable=False),
        sa.Column("method", method, nullable=False),
        sa.Column("status", reconciliation_status, server_default="PENDING", nullable=False),
        sa.Column("window_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint", sa.JSON(), nullable=True),
        sa.Column("result_summary", sa.JSON(), nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "window_ends_at IS NULL OR window_starts_at IS NULL OR window_ends_at >= window_starts_at",
            name="ck_payment_reconciliation_window_order",
        ),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key", name="uq_payment_reconciliation_run_key"),
    )
    for name, columns in (
        ("ix_payment_reconciliation_runs_provider_namespace", ["provider_namespace"]),
        ("ix_payment_reconciliation_runs_method", ["method"]),
        ("ix_payment_reconciliation_runs_status", ["status"]),
    ):
        op.create_index(name, "payment_reconciliation_runs", columns, unique=False)


def _create_settlements() -> None:
    op.create_table(
        "payment_settlements",
        sa.Column("payment_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("journal_group_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("provider_event_id", sa.Uuid(), nullable=True),
        sa.Column("manual_payment_proof_id", sa.Uuid(), nullable=True),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("verification_source", sa.String(length=100), nullable=False),
        sa.Column("settled_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("amount_paise > 0", name="ck_payment_settlement_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_payment_settlement_currency_inr"),
        sa.ForeignKeyConstraint(["payment_attempt_id"], ["payment_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["journal_group_id"], ["journal_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["idempotency_record_id"], ["idempotency_records.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["provider_event_id"], ["payment_provider_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["manual_payment_proof_id"], ["manual_payment_proofs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["settled_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payment_attempt_id", name="uq_payment_settlement_attempt"),
        sa.UniqueConstraint("order_id", name="uq_payment_settlement_order"),
        sa.UniqueConstraint("journal_group_id", name="uq_payment_settlement_journal"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_payment_settlement_idempotency"),
        sa.UniqueConstraint("provider_event_id", name="uq_payment_settlement_provider_event"),
        sa.UniqueConstraint("manual_payment_proof_id", name="uq_payment_settlement_manual_proof"),
    )


def _create_outbox_events() -> None:
    outbox_status = _column_enum("payment_outbox_event_status", "PENDING", "PROCESSING", "DELIVERED", "FAILED")
    op.create_table(
        "payment_outbox_events",
        sa.Column("payment_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("deduplication_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", outbox_status, server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["payment_attempt_id"], ["payment_attempts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key", name="uq_payment_outbox_deduplication"),
    )
    for name, columns in (
        ("ix_payment_outbox_events_payment_attempt_id", ["payment_attempt_id"]),
        ("ix_payment_outbox_events_aggregate_id", ["aggregate_id"]),
        ("ix_payment_outbox_events_event_type", ["event_type"]),
        ("ix_payment_outbox_events_status", ["status"]),
        ("ix_payment_outbox_events_available_at", ["available_at"]),
    ):
        op.create_index(name, "payment_outbox_events", columns, unique=False)


def _drop_outbox_events() -> None:
    for name in (
        "ix_payment_outbox_events_available_at",
        "ix_payment_outbox_events_status",
        "ix_payment_outbox_events_event_type",
        "ix_payment_outbox_events_aggregate_id",
        "ix_payment_outbox_events_payment_attempt_id",
    ):
        op.drop_index(name, table_name="payment_outbox_events")
    op.drop_table("payment_outbox_events")


def _drop_settlements() -> None:
    op.drop_table("payment_settlements")


def _drop_reconciliation_runs() -> None:
    for name in (
        "ix_payment_reconciliation_runs_status",
        "ix_payment_reconciliation_runs_method",
        "ix_payment_reconciliation_runs_provider_namespace",
    ):
        op.drop_index(name, table_name="payment_reconciliation_runs")
    op.drop_table("payment_reconciliation_runs")


def _drop_manual_proofs() -> None:
    for name in (
        "ix_manual_payment_proofs_status",
        "ix_manual_payment_proofs_buyer_user_id",
        "ix_manual_payment_proofs_manual_destination_id",
        "ix_manual_payment_proofs_payment_attempt_id",
    ):
        op.drop_index(name, table_name="manual_payment_proofs")
    op.drop_table("manual_payment_proofs")


def _drop_provider_events() -> None:
    for name in (
        "ix_payment_provider_events_status",
        "ix_payment_provider_events_provider_payment_reference",
        "ix_payment_provider_events_merchant_reference",
        "ix_payment_provider_events_payment_attempt_id",
    ):
        op.drop_index(name, table_name="payment_provider_events")
    op.drop_table("payment_provider_events")


def _drop_payment_attempts() -> None:
    op.drop_index("uq_payment_attempt_one_active_order", table_name="payment_attempts")
    for name in (
        "ix_payment_attempts_expires_at",
        "ix_payment_attempts_manual_destination_id",
        "ix_payment_attempts_status",
        "ix_payment_attempts_provider_namespace",
        "ix_payment_attempts_method",
        "ix_payment_attempts_buyer_user_id",
        "ix_payment_attempts_order_id",
    ):
        op.drop_index(name, table_name="payment_attempts")
    op.drop_table("payment_attempts")


def _drop_manual_destinations() -> None:
    op.drop_index("ix_manual_payment_destinations_status", table_name="manual_payment_destinations")
    op.drop_table("manual_payment_destinations")
