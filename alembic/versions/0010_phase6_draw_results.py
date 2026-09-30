"""add auditable draw, winner, award, and result-notification persistence

Revision ID: 0010_phase6_draw_results
Revises: 0009_phase5_manual_destination_uniqueness
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_phase6_draw_results"
down_revision = "0009_phase5_manual_destination_uniqueness"
branch_labels = None
depends_on = None


DRAW_STATUSES = ("COMMITTED", "SALES_CLOSED", "DRAWN", "PRIZES_POSTED", "RESULT_PUBLISHED")


def _draw_status_enum() -> sa.Enum:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*DRAW_STATUSES, name="draw_status", create_type=False)
    return sa.Enum(*DRAW_STATUSES, name="draw_status")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(*DRAW_STATUSES, name="draw_status").create(bind, checkfirst=True)

    draw_status = _draw_status_enum()
    op.create_table(
        "draws",
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("status", draw_status, server_default="COMMITTED", nullable=False),
        sa.Column("algorithm_version", sa.String(length=100), nullable=False),
        sa.Column("seed_commitment", sa.String(length=64), nullable=False),
        sa.Column("seed_reveal", sa.String(length=512), nullable=True),
        sa.Column("eligible_ticket_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("eligible_tickets_digest", sa.String(length=64), nullable=True),
        sa.Column("prize_snapshot", sa.JSON(), nullable=True),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("committed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drawn_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("drawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prizes_posted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("prizes_posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("eligible_ticket_count >= 0", name="ck_draw_eligible_ticket_count_nonnegative"),
        sa.CheckConstraint("length(seed_commitment) = 64", name="ck_draw_seed_commitment_sha256"),
        sa.ForeignKeyConstraint(["series_id"], ["ticket_series.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["committed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["closed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["drawn_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["prizes_posted_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", name="uq_draw_series"),
    )
    op.create_index("ix_draws_series_id", "draws", ["series_id"], unique=False)
    op.create_index("ix_draws_status", "draws", ["status"], unique=False)
    op.create_index("ix_draws_committed_by_user_id", "draws", ["committed_by_user_id"], unique=False)

    op.create_table(
        "draw_entries",
        sa.Column("draw_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("serial_number", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("serial_number > 0", name="ck_draw_entry_serial_positive"),
        sa.ForeignKeyConstraint(["draw_id"], ["draws.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draw_id", "ticket_id", name="uq_draw_entry_ticket"),
        sa.UniqueConstraint("draw_id", "serial_number", name="uq_draw_entry_serial"),
    )
    for name, columns in (
        ("ix_draw_entries_draw_id", ["draw_id"]),
        ("ix_draw_entries_ticket_id", ["ticket_id"]),
        ("ix_draw_entries_owner_user_id", ["owner_user_id"]),
    ):
        op.create_index(name, "draw_entries", columns, unique=False)

    op.create_table(
        "winners",
        sa.Column("draw_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("prize_paise", sa.BigInteger(), nullable=False),
        sa.Column("selection_counter", sa.Integer(), nullable=False),
        sa.Column("selection_digest", sa.String(length=64), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("rank > 0", name="ck_winner_rank_positive"),
        sa.CheckConstraint("prize_paise > 0", name="ck_winner_prize_positive"),
        sa.CheckConstraint("selection_counter >= 0", name="ck_winner_selection_counter_nonnegative"),
        sa.ForeignKeyConstraint(["draw_id"], ["draws.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draw_id", "rank", name="uq_winner_draw_rank"),
        sa.UniqueConstraint("draw_id", "ticket_id", name="uq_winner_draw_ticket"),
    )
    for name, columns in (
        ("ix_winners_draw_id", ["draw_id"]),
        ("ix_winners_ticket_id", ["ticket_id"]),
        ("ix_winners_owner_user_id", ["owner_user_id"]),
    ):
        op.create_index(name, "winners", columns, unique=False)

    op.create_table(
        "prize_awards",
        sa.Column("draw_id", sa.Uuid(), nullable=False),
        sa.Column("winner_id", sa.Uuid(), nullable=False),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("journal_group_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_record_id", sa.Uuid(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("credited_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("amount_paise > 0", name="ck_prize_award_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_prize_award_currency_inr"),
        sa.ForeignKeyConstraint(["draw_id"], ["draws.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["winner_id"], ["winners.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["journal_group_id"], ["journal_groups.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["idempotency_record_id"], ["idempotency_records.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["credited_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("winner_id", name="uq_prize_award_winner"),
        sa.UniqueConstraint("journal_group_id", name="uq_prize_award_journal_group"),
        sa.UniqueConstraint("idempotency_record_id", name="uq_prize_award_idempotency"),
    )
    for name, columns in (
        ("ix_prize_awards_draw_id", ["draw_id"]),
        ("ix_prize_awards_winner_id", ["winner_id"]),
        ("ix_prize_awards_wallet_id", ["wallet_id"]),
        ("ix_prize_awards_credited_by_user_id", ["credited_by_user_id"]),
    ):
        op.create_index(name, "prize_awards", columns, unique=False)

    op.create_table(
        "draw_notifications",
        sa.Column("draw_id", sa.Uuid(), nullable=False),
        sa.Column("winner_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(["draw_id"], ["draws.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["winner_id"], ["winners.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draw_id", "winner_id", "event_type", name="uq_draw_notification_event"),
    )
    for name, columns in (
        ("ix_draw_notifications_draw_id", ["draw_id"]),
        ("ix_draw_notifications_winner_id", ["winner_id"]),
        ("ix_draw_notifications_user_id", ["user_id"]),
    ):
        op.create_index(name, "draw_notifications", columns, unique=False)


def downgrade() -> None:
    for name in ("ix_draw_notifications_user_id", "ix_draw_notifications_winner_id", "ix_draw_notifications_draw_id"):
        op.drop_index(name, table_name="draw_notifications")
    op.drop_table("draw_notifications")

    for name in (
        "ix_prize_awards_credited_by_user_id",
        "ix_prize_awards_wallet_id",
        "ix_prize_awards_winner_id",
        "ix_prize_awards_draw_id",
    ):
        op.drop_index(name, table_name="prize_awards")
    op.drop_table("prize_awards")

    for name in ("ix_winners_owner_user_id", "ix_winners_ticket_id", "ix_winners_draw_id"):
        op.drop_index(name, table_name="winners")
    op.drop_table("winners")

    for name in ("ix_draw_entries_owner_user_id", "ix_draw_entries_ticket_id", "ix_draw_entries_draw_id"):
        op.drop_index(name, table_name="draw_entries")
    op.drop_table("draw_entries")

    for name in ("ix_draws_committed_by_user_id", "ix_draws_status", "ix_draws_series_id"):
        op.drop_index(name, table_name="draws")
    op.drop_table("draws")
    if op.get_bind().dialect.name == "postgresql":
        postgresql.ENUM(*DRAW_STATUSES, name="draw_status").drop(op.get_bind(), checkfirst=True)
