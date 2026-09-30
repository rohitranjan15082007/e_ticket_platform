"""add P2P withdrawal matching, evidence and safe settlement records

Revision ID: 0005_p2p_matching_settlement
Revises: 0004_ticket_catalog_orders
Create Date: 2026-09-22
"""

from uuid import UUID

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite


revision = "0005_p2p_matching_settlement"
down_revision = "0004_ticket_catalog_orders"
branch_labels = None
depends_on = None


PLATFORM_P2P_ORDER_PENDING_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000002")
PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE = "platform:p2p-order-funds-pending:inr"

UNRESOLVED_WITHDRAWALS = "status IN ('WAITING_FOR_BUYER', 'MATCHED', 'UNDER_REVIEW')"
ACTIVE_MATCHES = (
    "status IN ('WAITING_FOR_PAYMENT', 'PAYMENT_SUBMITTED', "
    "'WAITING_FOR_RECEIVER_CONFIRMATION', 'UNDER_VERIFICATION', "
    "'EXPIRED_AWAITING_RECONCILIATION', 'DISPUTED', 'ADMIN_REVIEW', 'REFUND_PENDING')"
)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        # PostgreSQL cannot safely use a just-added enum member in the same
        # migration transaction. Commit the enum extension before seeding rows.
        with op.get_context().autocommit_block():
            for enum_name, value in (
                ("order_status", "WAITING_FOR_MATCH"),
                ("order_status", "AWAITING_PAYMENT"),
                ("wallet_hold_status", "SETTLED"),
                ("ledger_account_kind", "PLATFORM_P2P_ORDER_PENDING"),
            ):
                op.execute(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'")

    _extend_wallet_hold_resolution()
    _seed_p2p_order_pending_account()
    _create_p2p_tables()


def downgrade() -> None:
    _drop_p2p_tables()
    _restore_wallet_hold_resolution()
    # PostgreSQL enum additions and the append-only seeded account are retained
    # intentionally. Removing them would require mutating immutable accounting
    # configuration; they are harmless when downgrading application tables.


def _extend_wallet_hold_resolution() -> None:
    with op.batch_alter_table("wallet_holds") as batch:
        batch.add_column(sa.Column("settlement_journal_group_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True))
        batch.drop_constraint("ck_wallet_hold_resolution", type_="check")
        batch.create_foreign_key(
            "fk_wallet_holds_settlement_journal",
            "journal_groups",
            ["settlement_journal_group_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_wallet_hold_settlement_journal", ["settlement_journal_group_id"]
        )
        batch.create_check_constraint(
            "ck_wallet_hold_resolution",
            "(status = 'ACTIVE' AND release_journal_group_id IS NULL AND released_at IS NULL "
            "AND settlement_journal_group_id IS NULL AND settled_at IS NULL) "
            "OR (status = 'RELEASED' AND release_journal_group_id IS NOT NULL AND released_at IS NOT NULL "
            "AND settlement_journal_group_id IS NULL AND settled_at IS NULL) "
            "OR (status = 'SETTLED' AND settlement_journal_group_id IS NOT NULL AND settled_at IS NOT NULL "
            "AND release_journal_group_id IS NULL AND released_at IS NULL)",
        )


def _restore_wallet_hold_resolution() -> None:
    with op.batch_alter_table("wallet_holds") as batch:
        batch.drop_constraint("ck_wallet_hold_resolution", type_="check")
        batch.drop_constraint("uq_wallet_hold_settlement_journal", type_="unique")
        batch.drop_constraint("fk_wallet_holds_settlement_journal", type_="foreignkey")
        batch.drop_column("settled_at")
        batch.drop_column("settlement_journal_group_id")
        batch.create_check_constraint(
            "ck_wallet_hold_resolution",
            "(status = 'ACTIVE' AND release_journal_group_id IS NULL AND released_at IS NULL) "
            "OR (status = 'RELEASED' AND release_journal_group_id IS NOT NULL AND released_at IS NOT NULL)",
        )


def _seed_p2p_order_pending_account() -> None:
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
        "id": PLATFORM_P2P_ORDER_PENDING_ACCOUNT_ID,
        "code": PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE,
        "kind": "PLATFORM_P2P_ORDER_PENDING",
        "currency": "INR",
        "wallet_id": None,
        "user_id": None,
    }
    bind = op.get_bind()

    # 0005's downgrade deliberately retains this append-only accounting
    # account.  A later re-upgrade therefore must not try to insert a second
    # copy.  Do not update an existing account: ledger_accounts is immutable.
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
        raise RuntimeError("P2P pending-account migration supports PostgreSQL and SQLite only")
    bind.execute(statement)

    # A conflict is acceptable only when the existing append-only account is
    # the exact canonical account.  Fail closed rather than silently accepting
    # a conflicting ledger configuration.  Offline SQL has no result rows to
    # inspect; the generated INSERT still remains idempotent.
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
                ledger_accounts.c.id == PLATFORM_P2P_ORDER_PENDING_ACCOUNT_ID,
                ledger_accounts.c.code == PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE,
            )
        )
    ).mappings().all()
    if len(rows) != 1:
        raise RuntimeError("P2P pending ledger account identity is not unique")

    account = rows[0]
    if (
        str(account["id"]) != str(PLATFORM_P2P_ORDER_PENDING_ACCOUNT_ID)
        or account["code"] != PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE
        or account["kind"] != "PLATFORM_P2P_ORDER_PENDING"
        or account["currency"] != "INR"
        or account["wallet_id"] is not None
        or account["user_id"] is not None
    ):
        raise RuntimeError("P2P pending ledger account conflicts with immutable canonical configuration")


def _create_p2p_tables() -> None:
    destination_status = sa.Enum("PENDING_REVIEW", "VERIFIED", "DISABLED", name="payment_destination_status")
    withdrawal_status = sa.Enum(
        "WAITING_FOR_BUYER", "MATCHED", "UNDER_REVIEW", "COMPLETED", "CANCELLED", name="withdrawal_status"
    )
    match_status = sa.Enum(
        "WAITING_FOR_PAYMENT", "PAYMENT_SUBMITTED", "WAITING_FOR_RECEIVER_CONFIRMATION",
        "UNDER_VERIFICATION", "EXPIRED_AWAITING_RECONCILIATION", "DISPUTED", "ADMIN_REVIEW",
        "SETTLED", "CLOSED_UNPAID", "REFUND_PENDING", "REFUNDED", name="p2p_match_status",
    )
    submission_status = sa.Enum(
        "UNVERIFIED", "MANUAL_REVIEW", "VERIFIED", "REJECTED", name="p2p_submission_verification_status"
    )
    confirmation_decision = sa.Enum("RECEIVED", "NOT_RECEIVED", name="receiver_confirmation_decision")
    dispute_status = sa.Enum("OPEN", "UNDER_REVIEW", "RESOLVED", name="p2p_dispute_status")
    admin_decision = sa.Enum("SETTLE", "CLOSE_UNPAID", "KEEP_IN_REVIEW", name="admin_resolution_decision")
    refund_status = sa.Enum("PENDING_EVIDENCE", "APPROVED", "PAID", "REJECTED", name="p2p_refund_status")
    outbox_status = sa.Enum("PENDING", "PROCESSING", "DELIVERED", "FAILED", name="p2p_outbox_event_status")

    op.create_table(
        "payment_destinations",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider_namespace", sa.String(length=64), nullable=False),
        sa.Column("display_label", sa.String(length=120), nullable=False),
        sa.Column("destination_data", sa.JSON(), nullable=False),
        sa.Column("status", destination_status, server_default="PENDING_REVIEW", nullable=False),
        sa.Column("verification_method", sa.String(length=100), nullable=True),
        sa.Column("verification_evidence_reference", sa.String(length=255), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint(
            "(status <> 'VERIFIED') OR (verification_method IS NOT NULL "
            "AND verification_evidence_reference IS NOT NULL AND verified_at IS NOT NULL)",
            name="ck_payment_destination_verified_evidence",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payment_destinations_user_id", "payment_destinations", ["user_id"], unique=False)
    op.create_index("ix_payment_destinations_status", "payment_destinations", ["status"], unique=False)

    op.create_table(
        "withdrawal_requests",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("payment_destination_id", sa.Uuid(), nullable=False),
        sa.Column("wallet_hold_id", sa.Uuid(), nullable=True),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("eligible_balance_snapshot_paise", sa.BigInteger(), nullable=False),
        sa.Column("max_amount_snapshot_paise", sa.BigInteger(), nullable=False),
        sa.Column("rule_snapshot", sa.JSON(), nullable=False),
        sa.Column("rule_version", sa.String(length=100), nullable=False),
        sa.Column("status", withdrawal_status, server_default="WAITING_FOR_BUYER", nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("amount_paise > 0", name="ck_withdrawal_request_amount_positive"),
        sa.CheckConstraint("eligible_balance_snapshot_paise >= 0", name="ck_withdrawal_eligible_nonnegative"),
        sa.CheckConstraint("max_amount_snapshot_paise >= 0", name="ck_withdrawal_max_nonnegative"),
        sa.CheckConstraint("amount_paise <= max_amount_snapshot_paise", name="ck_withdrawal_within_snapshot"),
        sa.CheckConstraint("currency = 'INR'", name="ck_withdrawal_currency_inr"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_destination_id"], ["payment_destinations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wallet_hold_id"], ["wallet_holds.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wallet_hold_id", name="uq_withdrawal_wallet_hold"),
    )
    for name, columns in (
        ("ix_withdrawal_requests_user_id", ["user_id"]),
        ("ix_withdrawal_requests_wallet_id", ["wallet_id"]),
        ("ix_withdrawal_requests_status", ["status"]),
    ):
        op.create_index(name, "withdrawal_requests", columns, unique=False)
    op.create_index(
        "uq_withdrawal_one_unresolved_user", "withdrawal_requests", ["user_id"], unique=True,
        postgresql_where=sa.text(UNRESOLVED_WITHDRAWALS), sqlite_where=sa.text(UNRESOLVED_WITHDRAWALS),
    )

    op.create_table(
        "p2p_matches",
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("withdrawal_id", sa.Uuid(), nullable=False),
        sa.Column("buyer_user_id", sa.Uuid(), nullable=False),
        sa.Column("receiver_user_id", sa.Uuid(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("destination_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", match_status, server_default="WAITING_FOR_PAYMENT", nullable=False),
        sa.Column("payment_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receiver_confirmation_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("instructions_exposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instructions_disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("amount_paise > 0", name="ck_p2p_match_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_p2p_match_currency_inr"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["withdrawal_id"], ["withdrawal_requests.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["buyer_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["receiver_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_p2p_matches_order_id", ["order_id"]),
        ("ix_p2p_matches_withdrawal_id", ["withdrawal_id"]),
        ("ix_p2p_matches_buyer_user_id", ["buyer_user_id"]),
        ("ix_p2p_matches_receiver_user_id", ["receiver_user_id"]),
        ("ix_p2p_matches_status", ["status"]),
        ("ix_p2p_matches_payment_deadline_at", ["payment_deadline_at"]),
        ("ix_p2p_matches_receiver_confirmation_deadline_at", ["receiver_confirmation_deadline_at"]),
    ):
        op.create_index(name, "p2p_matches", columns, unique=False)
    op.create_index(
        "uq_p2p_active_match_order", "p2p_matches", ["order_id"], unique=True,
        postgresql_where=sa.text(ACTIVE_MATCHES), sqlite_where=sa.text(ACTIVE_MATCHES),
    )
    op.create_index(
        "uq_p2p_active_match_withdrawal", "p2p_matches", ["withdrawal_id"], unique=True,
        postgresql_where=sa.text(ACTIVE_MATCHES), sqlite_where=sa.text(ACTIVE_MATCHES),
    )

    op.create_table(
        "p2p_payment_submissions",
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("buyer_user_id", sa.Uuid(), nullable=False),
        sa.Column("provider_namespace", sa.String(length=64), nullable=False),
        sa.Column("claimed_reference", sa.String(length=160), nullable=False),
        sa.Column("expected_amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("expected_currency", sa.String(length=3), nullable=False),
        sa.Column("observed_amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("observed_currency", sa.String(length=3), nullable=False),
        sa.Column("declared_paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_upload_id", sa.Uuid(), nullable=True),
        sa.Column("provider_evidence_reference", sa.String(length=255), nullable=True),
        sa.Column("evidence_metadata", sa.JSON(), nullable=True),
        sa.Column("verification_status", submission_status, server_default="UNVERIFIED", nullable=False),
        sa.Column("verification_reason", sa.String(length=500), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_late", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("expected_amount_paise > 0", name="ck_p2p_submission_expected_positive"),
        sa.CheckConstraint("observed_amount_paise > 0", name="ck_p2p_submission_observed_positive"),
        sa.CheckConstraint("expected_currency = 'INR'", name="ck_p2p_submission_expected_inr"),
        sa.CheckConstraint("observed_currency = 'INR'", name="ck_p2p_submission_observed_inr"),
        sa.ForeignKeyConstraint(["match_id"], ["p2p_matches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["buyer_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", "provider_namespace", "claimed_reference", name="uq_p2p_submission_match_reference"),
    )
    op.create_index("ix_p2p_payment_submissions_match_id", "p2p_payment_submissions", ["match_id"], unique=False)
    op.create_index("ix_p2p_payment_submissions_verification_status", "p2p_payment_submissions", ["verification_status"], unique=False)

    op.create_table(
        "p2p_verified_payment_references",
        sa.Column("payment_submission_id", sa.Uuid(), nullable=False),
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("provider_namespace", sa.String(length=64), nullable=False),
        sa.Column("transaction_reference", sa.String(length=160), nullable=False),
        sa.Column("verification_source", sa.String(length=100), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["payment_submission_id"], ["p2p_payment_submissions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["match_id"], ["p2p_matches.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_namespace", "transaction_reference", name="uq_p2p_verified_reference"),
        sa.UniqueConstraint("payment_submission_id", name="uq_p2p_verified_reference_submission"),
    )
    op.create_index("ix_p2p_verified_payment_references_match_id", "p2p_verified_payment_references", ["match_id"], unique=False)

    op.create_table(
        "p2p_receiver_confirmations",
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("receiver_user_id", sa.Uuid(), nullable=False),
        sa.Column("decision", confirmation_decision, nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["p2p_matches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["receiver_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", name="uq_p2p_receiver_confirmation_match"),
    )
    op.create_index("ix_p2p_receiver_confirmations_match_id", "p2p_receiver_confirmations", ["match_id"], unique=False)

    op.create_table(
        "p2p_disputes",
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("opened_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("status", dispute_status, server_default="OPEN", nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("evidence_reference", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["p2p_matches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["opened_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", name="uq_p2p_dispute_match"),
    )
    op.create_index("ix_p2p_disputes_match_id", "p2p_disputes", ["match_id"], unique=False)
    op.create_index("ix_p2p_disputes_status", "p2p_disputes", ["status"], unique=False)

    op.create_table(
        "p2p_admin_resolutions",
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("dispute_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("decision", admin_decision, nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("evidence_reference", sa.String(length=255), nullable=False),
        sa.Column("verification_source", sa.String(length=100), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["p2p_matches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dispute_id"], ["p2p_disputes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_p2p_admin_resolutions_match_id", "p2p_admin_resolutions", ["match_id"], unique=False)
    op.create_index("ix_p2p_admin_resolutions_dispute_id", "p2p_admin_resolutions", ["dispute_id"], unique=False)

    op.create_table(
        "p2p_settlements",
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("withdrawal_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("journal_group_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("verification_source", sa.String(length=100), nullable=False),
        sa.Column("settled_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("amount_paise > 0", name="ck_p2p_settlement_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_p2p_settlement_currency_inr"),
        sa.ForeignKeyConstraint(["match_id"], ["p2p_matches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["withdrawal_id"], ["withdrawal_requests.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["journal_group_id"], ["journal_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["idempotency_record_id"], ["idempotency_records.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["settled_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", name="uq_p2p_settlement_match"),
        sa.UniqueConstraint("withdrawal_id", name="uq_p2p_settlement_withdrawal"),
        sa.UniqueConstraint("order_id", name="uq_p2p_settlement_order"),
        sa.UniqueConstraint("journal_group_id", name="uq_p2p_settlement_journal"),
    )

    op.create_table(
        "p2p_refunds",
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("settlement_id", sa.Uuid(), nullable=True),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("funding_source_reference", sa.String(length=255), nullable=True),
        sa.Column("destination_validation_reference", sa.String(length=255), nullable=True),
        sa.Column("payout_reference", sa.String(length=255), nullable=True),
        sa.Column("status", refund_status, server_default="PENDING_EVIDENCE", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("amount_paise > 0", name="ck_p2p_refund_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_p2p_refund_currency_inr"),
        sa.ForeignKeyConstraint(["match_id"], ["p2p_matches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["settlement_id"], ["p2p_settlements.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_p2p_refunds_match_id", "p2p_refunds", ["match_id"], unique=False)
    op.create_index("ix_p2p_refunds_settlement_id", "p2p_refunds", ["settlement_id"], unique=False)
    op.create_index("ix_p2p_refunds_status", "p2p_refunds", ["status"], unique=False)

    op.create_table(
        "p2p_outbox_events",
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("deduplication_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", outbox_status, server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key", name="uq_p2p_outbox_deduplication"),
    )
    op.create_index("ix_p2p_outbox_events_aggregate_id", "p2p_outbox_events", ["aggregate_id"], unique=False)
    op.create_index("ix_p2p_outbox_events_event_type", "p2p_outbox_events", ["event_type"], unique=False)
    op.create_index("ix_p2p_outbox_events_status", "p2p_outbox_events", ["status"], unique=False)


def _drop_p2p_tables() -> None:
    for index_name in ("ix_p2p_outbox_events_status", "ix_p2p_outbox_events_event_type", "ix_p2p_outbox_events_aggregate_id"):
        op.drop_index(index_name, table_name="p2p_outbox_events")
    op.drop_table("p2p_outbox_events")
    for index_name in ("ix_p2p_refunds_status", "ix_p2p_refunds_settlement_id", "ix_p2p_refunds_match_id"):
        op.drop_index(index_name, table_name="p2p_refunds")
    op.drop_table("p2p_refunds")
    op.drop_table("p2p_settlements")
    for index_name in ("ix_p2p_admin_resolutions_dispute_id", "ix_p2p_admin_resolutions_match_id"):
        op.drop_index(index_name, table_name="p2p_admin_resolutions")
    op.drop_table("p2p_admin_resolutions")
    for index_name in ("ix_p2p_disputes_status", "ix_p2p_disputes_match_id"):
        op.drop_index(index_name, table_name="p2p_disputes")
    op.drop_table("p2p_disputes")
    op.drop_index("ix_p2p_receiver_confirmations_match_id", table_name="p2p_receiver_confirmations")
    op.drop_table("p2p_receiver_confirmations")
    op.drop_index("ix_p2p_verified_payment_references_match_id", table_name="p2p_verified_payment_references")
    op.drop_table("p2p_verified_payment_references")
    for index_name in ("ix_p2p_payment_submissions_verification_status", "ix_p2p_payment_submissions_match_id"):
        op.drop_index(index_name, table_name="p2p_payment_submissions")
    op.drop_table("p2p_payment_submissions")
    for index_name in (
        "uq_p2p_active_match_withdrawal", "uq_p2p_active_match_order",
        "ix_p2p_matches_receiver_confirmation_deadline_at", "ix_p2p_matches_payment_deadline_at",
        "ix_p2p_matches_status", "ix_p2p_matches_receiver_user_id", "ix_p2p_matches_buyer_user_id",
        "ix_p2p_matches_withdrawal_id", "ix_p2p_matches_order_id",
    ):
        op.drop_index(index_name, table_name="p2p_matches")
    op.drop_table("p2p_matches")
    op.drop_index("uq_withdrawal_one_unresolved_user", table_name="withdrawal_requests")
    for index_name in ("ix_withdrawal_requests_status", "ix_withdrawal_requests_wallet_id", "ix_withdrawal_requests_user_id"):
        op.drop_index(index_name, table_name="withdrawal_requests")
    op.drop_table("withdrawal_requests")
    for index_name in ("ix_payment_destinations_status", "ix_payment_destinations_user_id"):
        op.drop_index(index_name, table_name="payment_destinations")
    op.drop_table("payment_destinations")
    if op.get_bind().dialect.name == "postgresql":
        for enum_name in (
            "p2p_outbox_event_status", "p2p_refund_status", "admin_resolution_decision",
            "p2p_dispute_status", "receiver_confirmation_decision", "p2p_submission_verification_status",
            "p2p_match_status", "withdrawal_status", "payment_destination_status",
        ):
            op.execute(f"DROP TYPE IF EXISTS {enum_name}")
