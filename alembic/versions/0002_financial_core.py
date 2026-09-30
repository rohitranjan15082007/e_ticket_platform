"""create wallet, ledger and audit tables

Revision ID: 0002_financial_core
Revises: 0001_foundation_identity
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_financial_core"
down_revision = "0001_foundation_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("available_paise", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("locked_paise", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("available_paise >= 0", name="ck_wallet_available_nonnegative"),
        sa.CheckConstraint("locked_paise >= 0", name="ck_wallet_locked_nonnegative"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_table(
        "ledger_accounts",
        sa.Column("code", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.Enum("USER_AVAILABLE", "USER_WITHDRAWAL_HELD", "PLATFORM_CLEARING", name="ledger_account_kind"), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("wallet_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wallet_id", "kind", name="uq_ledger_account_wallet_kind"),
    )
    op.create_index("ix_ledger_accounts_code", "ledger_accounts", ["code"], unique=True)
    op.create_table(
        "journal_groups",
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reference_type", sa.String(length=100), nullable=False),
        sa.Column("reference_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["idempotency_record_id"], ["idempotency_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_record_id"),
    )
    op.create_index("ix_journal_groups_event_type", "journal_groups", ["event_type"], unique=False)
    op.create_table(
        "journal_postings",
        sa.Column("journal_group_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.Enum("DEBIT", "CREDIT", name="posting_direction"), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("amount_paise > 0", name="ck_journal_posting_amount_positive"),
        sa.ForeignKeyConstraint(["account_id"], ["ledger_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["journal_group_id"], ["journal_groups.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_journal_postings_journal_group_id", "journal_postings", ["journal_group_id"], unique=False)
    op.create_table(
        "audit_logs",
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("entity_type", sa.String(length=100), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
    op.create_index("ix_audit_logs_entity_id", "audit_logs", ["entity_id"], unique=False)
    op.create_index("ix_audit_logs_entity_type", "audit_logs", ["entity_type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_audit_logs_entity_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_journal_postings_journal_group_id", table_name="journal_postings")
    op.drop_table("journal_postings")
    op.drop_index("ix_journal_groups_event_type", table_name="journal_groups")
    op.drop_table("journal_groups")
    op.drop_index("ix_ledger_accounts_code", table_name="ledger_accounts")
    op.drop_table("ledger_accounts")
    op.drop_table("wallets")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE posting_direction")
        op.execute("DROP TYPE ledger_account_kind")
