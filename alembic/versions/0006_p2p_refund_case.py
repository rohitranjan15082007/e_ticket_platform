"""add evidence-bound P2P refund case fields

Revision ID: 0006_p2p_refund_case
Revises: 0005_p2p_matching_settlement
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_p2p_refund_case"
down_revision = "0005_p2p_matching_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No route in 0005 could create a refund, so a populated table means an
    # operator inserted records outside the supported workflow. Refuse to
    # invent evidence/actors for those rows during an integrity migration.
    if not op.get_context().as_sql:
        existing_rows = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM p2p_refunds")).scalar_one()
        if existing_rows:
            raise RuntimeError(
                "Cannot upgrade populated p2p_refunds without manual evidence migration; no defaults are safe"
            )
    with op.batch_alter_table("p2p_refunds") as batch:
        batch.add_column(sa.Column("verified_payment_reference_id", sa.Uuid(), nullable=False))
        batch.add_column(sa.Column("liable_party", sa.String(length=100), nullable=False))
        batch.add_column(sa.Column("executor_reference", sa.String(length=255), nullable=False))
        batch.add_column(sa.Column("created_by_user_id", sa.Uuid(), nullable=False))
        batch.add_column(sa.Column("payout_verified_by_user_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("payout_verified_at", sa.DateTime(timezone=True), nullable=True))
        batch.alter_column("funding_source_reference", existing_type=sa.String(length=255), nullable=False)
        batch.alter_column("destination_validation_reference", existing_type=sa.String(length=255), nullable=False)
        batch.create_foreign_key(
            "fk_p2p_refunds_verified_payment_reference",
            "p2p_verified_payment_references",
            ["verified_payment_reference_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_p2p_refunds_created_by_user", "users", ["created_by_user_id"], ["id"], ondelete="RESTRICT"
        )
        batch.create_foreign_key(
            "fk_p2p_refunds_payout_verified_by_user",
            "users",
            ["payout_verified_by_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint("uq_p2p_refund_match", ["match_id"])
        batch.create_check_constraint(
            "ck_p2p_refund_paid_verification",
            "status <> 'PAID' OR (payout_reference IS NOT NULL "
            "AND payout_verified_by_user_id IS NOT NULL AND payout_verified_at IS NOT NULL)",
        )
    op.create_index(
        "ix_p2p_refunds_verified_payment_reference_id",
        "p2p_refunds",
        ["verified_payment_reference_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_p2p_refunds_verified_payment_reference_id", table_name="p2p_refunds")
    with op.batch_alter_table("p2p_refunds") as batch:
        batch.drop_constraint("ck_p2p_refund_paid_verification", type_="check")
        batch.drop_constraint("uq_p2p_refund_match", type_="unique")
        batch.drop_constraint("fk_p2p_refunds_payout_verified_by_user", type_="foreignkey")
        batch.drop_constraint("fk_p2p_refunds_created_by_user", type_="foreignkey")
        batch.drop_constraint("fk_p2p_refunds_verified_payment_reference", type_="foreignkey")
        batch.alter_column("destination_validation_reference", existing_type=sa.String(length=255), nullable=True)
        batch.alter_column("funding_source_reference", existing_type=sa.String(length=255), nullable=True)
        batch.drop_column("payout_verified_at")
        batch.drop_column("payout_verified_by_user_id")
        batch.drop_column("created_by_user_id")
        batch.drop_column("executor_reference")
        batch.drop_column("liable_party")
        batch.drop_column("verified_payment_reference_id")
