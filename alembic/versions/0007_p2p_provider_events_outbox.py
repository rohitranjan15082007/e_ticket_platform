"""add signed P2P provider-event and in-app notification persistence

Revision ID: 0007_p2p_provider_events_outbox
Revises: 0006_p2p_refund_case
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_p2p_provider_events_outbox"
down_revision = "0006_p2p_refund_case"
branch_labels = None
depends_on = None


def upgrade() -> None:
    provider_event_status = sa.Enum(
        "RECEIVED",
        "PROCESSING",
        "PROCESSED",
        "REVIEW_REQUIRED",
        "FAILED",
        name="p2p_provider_event_status",
    )
    op.create_table(
        "p2p_provider_events",
        sa.Column("provider_namespace", sa.String(length=64), nullable=False),
        sa.Column("external_event_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("match_id", sa.Uuid(), nullable=True),
        sa.Column("payment_submission_id", sa.Uuid(), nullable=True),
        sa.Column("transaction_reference", sa.String(length=160), nullable=True),
        sa.Column("verified_amount_paise", sa.BigInteger(), nullable=True),
        sa.Column("verified_currency", sa.String(length=3), nullable=True),
        sa.Column("recipient_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", provider_event_status, server_default="RECEIVED", nullable=False),
        sa.Column("processing_error", sa.String(length=500), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint(
            "verified_amount_paise IS NULL OR verified_amount_paise > 0",
            name="ck_p2p_provider_event_amount_positive",
        ),
        sa.CheckConstraint(
            "verified_currency IS NULL OR verified_currency = 'INR'",
            name="ck_p2p_provider_event_currency_inr",
        ),
        sa.ForeignKeyConstraint(["match_id"], ["p2p_matches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["payment_submission_id"], ["p2p_payment_submissions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_namespace", "external_event_id", name="uq_p2p_provider_event_external"),
    )
    op.create_index("ix_p2p_provider_events_match_id", "p2p_provider_events", ["match_id"], unique=False)
    op.create_index(
        "ix_p2p_provider_events_payment_submission_id",
        "p2p_provider_events",
        ["payment_submission_id"],
        unique=False,
    )
    op.create_index(
        "ix_p2p_provider_events_transaction_reference",
        "p2p_provider_events",
        ["transaction_reference"],
        unique=False,
    )
    op.create_index("ix_p2p_provider_events_status", "p2p_provider_events", ["status"], unique=False)

    op.create_table(
        "p2p_notifications",
        sa.Column("outbox_event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["outbox_event_id"], ["p2p_outbox_events.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outbox_event_id", "user_id", name="uq_p2p_notification_outbox_user"),
    )
    op.create_index("ix_p2p_notifications_outbox_event_id", "p2p_notifications", ["outbox_event_id"], unique=False)
    op.create_index("ix_p2p_notifications_user_id", "p2p_notifications", ["user_id"], unique=False)
    op.create_index("ix_p2p_notifications_event_type", "p2p_notifications", ["event_type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_p2p_notifications_event_type", table_name="p2p_notifications")
    op.drop_index("ix_p2p_notifications_user_id", table_name="p2p_notifications")
    op.drop_index("ix_p2p_notifications_outbox_event_id", table_name="p2p_notifications")
    op.drop_table("p2p_notifications")

    op.drop_index("ix_p2p_provider_events_status", table_name="p2p_provider_events")
    op.drop_index("ix_p2p_provider_events_transaction_reference", table_name="p2p_provider_events")
    op.drop_index("ix_p2p_provider_events_payment_submission_id", table_name="p2p_provider_events")
    op.drop_index("ix_p2p_provider_events_match_id", table_name="p2p_provider_events")
    op.drop_table("p2p_provider_events")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS p2p_provider_event_status")
