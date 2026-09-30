"""add financial hold lifecycle and PostgreSQL accounting guards

Revision ID: 0003_financial_integrity
Revises: 0002_financial_core
Create Date: 2026-09-21
"""

from uuid import UUID

from alembic import op
import sqlalchemy as sa


revision = "0003_financial_integrity"
down_revision = "0002_financial_core"
branch_labels = None
depends_on = None


PLATFORM_CLEARING_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000001")
PLATFORM_CLEARING_ACCOUNT_CODE = "platform:clearing:inr"


def upgrade() -> None:
    op.add_column("idempotency_records", sa.Column("response_payload", sa.JSON(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            UPDATE journal_groups
            SET idempotency_record_id = idempotency_records.id
            FROM idempotency_records
            WHERE journal_groups.idempotency_record_id IS NULL
              AND idempotency_records.resource_type = 'journal_group'
              AND idempotency_records.resource_id = journal_groups.id
            """
        )
        op.alter_column("journal_groups", "idempotency_record_id", nullable=False)
    op.create_table(
        "wallet_holds",
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("reference_type", sa.String(length=100), nullable=False),
        sa.Column("reference_id", sa.Uuid(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "RELEASED", name="wallet_hold_status"),
            nullable=False,
        ),
        sa.Column("hold_journal_group_id", sa.Uuid(), nullable=False),
        sa.Column("release_journal_group_id", sa.Uuid(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_paise > 0", name="ck_wallet_hold_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_wallet_hold_currency_inr"),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND release_journal_group_id IS NULL AND released_at IS NULL) "
            "OR (status = 'RELEASED' AND release_journal_group_id IS NOT NULL AND released_at IS NOT NULL)",
            name="ck_wallet_hold_resolution",
        ),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["hold_journal_group_id"], ["journal_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["release_journal_group_id"], ["journal_groups.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wallet_id", "reference_type", "reference_id", name="uq_wallet_hold_reference"),
        sa.UniqueConstraint("hold_journal_group_id", name="uq_wallet_hold_journal"),
        sa.UniqueConstraint("release_journal_group_id", name="uq_wallet_hold_release_journal"),
    )
    op.create_index("ix_wallet_holds_wallet_id", "wallet_holds", ["wallet_id"], unique=False)
    _seed_inr_clearing_account()
    if op.get_bind().dialect.name == "postgresql":
        _add_postgresql_currency_constraints()
        _install_postgresql_accounting_guards()


def downgrade() -> None:
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "postgresql":
        _remove_postgresql_accounting_guards()
        _drop_postgresql_currency_constraints()
    op.drop_index("ix_wallet_holds_wallet_id", table_name="wallet_holds")
    op.drop_table("wallet_holds")
    if dialect_name == "postgresql":
        op.execute("DROP TYPE wallet_hold_status")
    op.drop_column("idempotency_records", "response_payload")


def _seed_inr_clearing_account() -> None:
    ledger_accounts = sa.table(
        "ledger_accounts",
        sa.column("id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("kind", sa.Enum("USER_AVAILABLE", "USER_WITHDRAWAL_HELD", "PLATFORM_CLEARING", name="ledger_account_kind")),
        sa.column("currency", sa.String()),
        sa.column("wallet_id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
    )
    op.bulk_insert(
        ledger_accounts,
        [
            {
                "id": PLATFORM_CLEARING_ACCOUNT_ID,
                "code": PLATFORM_CLEARING_ACCOUNT_CODE,
                "kind": "PLATFORM_CLEARING",
                "currency": "INR",
                "wallet_id": None,
                "user_id": None,
            }
        ],
    )


def _add_postgresql_currency_constraints() -> None:
    op.create_check_constraint("ck_wallet_currency_inr", "wallets", "currency = 'INR'")
    op.create_check_constraint("ck_ledger_account_currency_inr", "ledger_accounts", "currency = 'INR'")
    op.create_check_constraint("ck_journal_group_currency_inr", "journal_groups", "currency = 'INR'")
    op.create_check_constraint("ck_journal_posting_currency_inr", "journal_postings", "currency = 'INR'")


def _drop_postgresql_currency_constraints() -> None:
    op.drop_constraint("ck_journal_posting_currency_inr", "journal_postings", type_="check")
    op.drop_constraint("ck_journal_group_currency_inr", "journal_groups", type_="check")
    op.drop_constraint("ck_ledger_account_currency_inr", "ledger_accounts", type_="check")
    op.drop_constraint("ck_wallet_currency_inr", "wallets", type_="check")


def _install_postgresql_accounting_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION enforce_journal_group_integrity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            target_group_id uuid;
            group_currency varchar(3);
            debit_total bigint;
            credit_total bigint;
            posting_count bigint;
        BEGIN
            IF TG_TABLE_NAME = 'journal_groups' THEN
                target_group_id := NEW.id;
            ELSIF TG_OP = 'DELETE' THEN
                target_group_id := OLD.journal_group_id;
            ELSE
                target_group_id := NEW.journal_group_id;
            END IF;

            SELECT currency INTO group_currency
            FROM journal_groups
            WHERE id = target_group_id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;

            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN direction = 'DEBIT' THEN amount_paise ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN direction = 'CREDIT' THEN amount_paise ELSE 0 END), 0)
            INTO posting_count, debit_total, credit_total
            FROM journal_postings
            WHERE journal_group_id = target_group_id;

            IF posting_count < 2 OR debit_total <> credit_total THEN
                RAISE EXCEPTION 'journal group % is not balanced', target_group_id
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM journal_postings posting
                JOIN ledger_accounts account ON account.id = posting.account_id
                WHERE posting.journal_group_id = target_group_id
                  AND (posting.currency <> group_currency OR account.currency <> group_currency)
            ) THEN
                RAISE EXCEPTION 'journal group % has inconsistent account or posting currency', target_group_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION prohibit_financial_history_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER journal_group_must_balance
        AFTER INSERT ON journal_groups
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION enforce_journal_group_integrity();
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER journal_postings_must_preserve_balance
        AFTER INSERT OR UPDATE OR DELETE ON journal_postings
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION enforce_journal_group_integrity();
        """
    )
    for table_name in ("journal_groups", "journal_postings", "audit_logs", "ledger_accounts"):
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_append_only
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION prohibit_financial_history_mutation();
            """
        )


def _remove_postgresql_accounting_guards() -> None:
    for table_name in ("journal_groups", "journal_postings", "audit_logs", "ledger_accounts"):
        op.execute(f"DROP TRIGGER {table_name}_append_only ON {table_name}")
    op.execute("DROP TRIGGER journal_postings_must_preserve_balance ON journal_postings")
    op.execute("DROP TRIGGER journal_group_must_balance ON journal_groups")
    op.execute("DROP FUNCTION prohibit_financial_history_mutation()")
    op.execute("DROP FUNCTION enforce_journal_group_integrity()")
