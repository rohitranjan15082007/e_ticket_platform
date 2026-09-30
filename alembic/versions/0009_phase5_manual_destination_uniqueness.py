"""enforce one active manual payment destination

Revision ID: 0009_phase5_manual_destination_uniqueness
Revises: 0008_phase5_payment_core
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_phase5_manual_destination_uniqueness"
down_revision = "0008_phase5_payment_core"
branch_labels = None
depends_on = None


ACTIVE_DESTINATION_INDEX = "uq_manual_payment_destination_one_active"
ACTIVE_DESTINATION_PREDICATE = "status = 'ACTIVE'"


def upgrade() -> None:
    """Reject ambiguous existing data before enforcing the active singleton."""

    # Alembic creates version_num as VARCHAR(32), but this revision identifier
    # is longer. Widen metadata before Alembic records the completed revision.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")

    # Offline SQL generation has no database state to inspect. Keep the
    # protective reconciliation check for online upgrades, while still
    # allowing deployment tooling to render the complete PostgreSQL chain.
    if not op.get_context().as_sql:
        active_count = op.get_bind().scalar(
            sa.text("SELECT COUNT(*) FROM manual_payment_destinations WHERE status = 'ACTIVE'")
        )
        if active_count is not None and int(active_count) > 1:
            raise RuntimeError(
                "Cannot enforce one active manual payment destination: disable or reconcile duplicate ACTIVE rows first"
            )
    op.create_index(
        ACTIVE_DESTINATION_INDEX,
        "manual_payment_destinations",
        ["status"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_DESTINATION_PREDICATE),
        sqlite_where=sa.text(ACTIVE_DESTINATION_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(ACTIVE_DESTINATION_INDEX, table_name="manual_payment_destinations")
