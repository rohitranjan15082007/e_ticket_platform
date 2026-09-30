"""add Phase 7 referral, cashback and affiliate review records

Revision ID: 0013_phase7_marketing_rewards
Revises: 0012_phase7_coupon_revenue
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0013_phase7_marketing_rewards"
down_revision = "0012_phase7_coupon_revenue"
branch_labels = None
depends_on = None

ENUMS = {
    "referral_program_status": ("DRAFT", "ACTIVE", "DISABLED"),
    "referral_status": ("PENDING", "QUALIFIED", "VOIDED"),
    "referral_reward_recipient": ("REFERRER", "REFERRED"),
    "referral_reward_status": ("PENDING_REVIEW", "VOIDED"),
    "cashback_campaign_status": ("DRAFT", "ACTIVE", "DISABLED"),
    "cashback_reward_type": ("FIXED_PAISE", "PERCENT_BPS"),
    "cashback_reward_status": ("PENDING_REVIEW", "VOIDED"),
    "affiliate_status": ("PENDING", "ACTIVE", "SUSPENDED"),
    "affiliate_commission_type": ("FIXED_PAISE", "PERCENT_BPS"),
    "affiliate_conversion_status": ("PENDING_REVIEW", "VOIDED"),
    "affiliate_commission_status": ("PENDING_REVIEW", "VOIDED"),
}


def _enum(name: str) -> sa.Enum:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)
    return sa.Enum(*ENUMS[name], name=name)


def _base() -> tuple[sa.Column, ...]:
    return (
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )


def _fk(column: str, target: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(column, sa.Uuid(), sa.ForeignKey(target, ondelete="RESTRICT"), nullable=nullable)


def _idx(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for name, values in ENUMS.items():
            postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "referral_profiles",
        *_base(),
        _fk("user_id", "users.id"),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_referral_profile_user"),
        sa.UniqueConstraint("code", name="uq_referral_profile_code"),
        sa.CheckConstraint("code = upper(code)", name="ck_referral_profile_code_uppercase"),
    )
    _idx("referral_profiles", "user_id", "code", "is_active")

    op.create_table(
        "referral_programs",
        *_base(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500)),
        sa.Column("referrer_reward_paise", sa.BigInteger(), nullable=False),
        sa.Column("referred_reward_paise", sa.BigInteger(), nullable=False),
        sa.Column("minimum_order_paise", sa.BigInteger(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("status", _enum("referral_program_status"), server_default="DRAFT", nullable=False),
        _fk("created_by_user_id", "users.id"),
        _fk("activated_by_user_id", "users.id", nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("referrer_reward_paise > 0", name="ck_referral_program_referrer_positive"),
        sa.CheckConstraint("referred_reward_paise > 0", name="ck_referral_program_referred_positive"),
        sa.CheckConstraint("minimum_order_paise > 0", name="ck_referral_program_minimum_positive"),
        sa.CheckConstraint("ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at", name="ck_referral_program_window"),
    )
    _idx("referral_programs", "status", "created_by_user_id")
    op.create_index(
        "uq_referral_program_one_active", "referral_programs", ["status"], unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"), sqlite_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "referrals",
        *_base(),
        _fk("program_id", "referral_programs.id"),
        _fk("referrer_user_id", "users.id"),
        _fk("referred_user_id", "users.id"),
        sa.Column("referral_code_snapshot", sa.String(64), nullable=False),
        sa.Column("program_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", _enum("referral_status"), server_default="PENDING", nullable=False),
        _fk("qualifying_order_id", "orders.id", nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qualified_at", sa.DateTime(timezone=True)),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column("void_reason", sa.String(500)),
        sa.UniqueConstraint("referred_user_id", name="uq_referral_referred_user"),
        sa.UniqueConstraint("qualifying_order_id", name="uq_referral_qualifying_order"),
        sa.CheckConstraint(
            "(status = 'PENDING' AND qualifying_order_id IS NULL AND qualified_at IS NULL AND voided_at IS NULL) "
            "OR (status = 'QUALIFIED' AND qualifying_order_id IS NOT NULL AND qualified_at IS NOT NULL AND voided_at IS NULL) "
            "OR (status = 'VOIDED' AND voided_at IS NOT NULL)",
            name="ck_referral_status_timestamps",
        ),
    )
    _idx("referrals", "referrer_user_id", "referred_user_id", "status")

    op.create_table(
        "referral_rewards",
        *_base(),
        _fk("referral_id", "referrals.id"),
        _fk("qualifying_order_id", "orders.id"),
        _fk("recipient_user_id", "users.id"),
        sa.Column("recipient", _enum("referral_reward_recipient"), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=False),
        sa.Column("status", _enum("referral_reward_status"), server_default="PENDING_REVIEW", nullable=False),
        sa.Column("created_from_settlement_id", sa.Uuid(), nullable=False),
        _fk("voided_by_user_id", "users.id", nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column("void_reason", sa.String(500)),
        sa.UniqueConstraint("referral_id", "recipient", name="uq_referral_reward_recipient"),
        sa.CheckConstraint("amount_paise > 0", name="ck_referral_reward_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_referral_reward_currency_inr"),
        sa.CheckConstraint(
            "(status = 'PENDING_REVIEW' AND voided_at IS NULL) OR (status = 'VOIDED' AND voided_at IS NOT NULL)",
            name="ck_referral_reward_status_timestamps",
        ),
    )
    _idx("referral_rewards", "referral_id", "qualifying_order_id", "recipient_user_id", "status")

    op.create_table(
        "cashback_campaigns",
        *_base(),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500)),
        sa.Column("reward_type", _enum("cashback_reward_type"), nullable=False),
        sa.Column("fixed_reward_paise", sa.BigInteger()),
        sa.Column("percentage_bps", sa.Integer()),
        sa.Column("max_reward_paise", sa.BigInteger()),
        sa.Column("minimum_order_paise", sa.BigInteger(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("status", _enum("cashback_campaign_status"), server_default="DRAFT", nullable=False),
        _fk("created_by_user_id", "users.id"),
        _fk("activated_by_user_id", "users.id", nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("code", name="uq_cashback_campaigns_code"),
        sa.CheckConstraint("code = upper(code)", name="ck_cashback_campaign_code_uppercase"),
        sa.CheckConstraint("minimum_order_paise > 0", name="ck_cashback_campaign_minimum_positive"),
        sa.CheckConstraint("ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at", name="ck_cashback_campaign_window"),
        sa.CheckConstraint(
            "(reward_type = 'FIXED_PAISE' AND fixed_reward_paise IS NOT NULL AND fixed_reward_paise > 0 "
            "AND percentage_bps IS NULL AND max_reward_paise IS NULL) OR "
            "(reward_type = 'PERCENT_BPS' AND percentage_bps IS NOT NULL AND percentage_bps > 0 "
            "AND percentage_bps <= 10000 AND fixed_reward_paise IS NULL "
            "AND (max_reward_paise IS NULL OR max_reward_paise > 0))",
            name="ck_cashback_campaign_definition",
        ),
    )
    _idx("cashback_campaigns", "code", "status", "created_by_user_id")
    op.create_index(
        "uq_cashback_campaign_one_active", "cashback_campaigns", ["status"], unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"), sqlite_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "cashback_rewards",
        *_base(),
        _fk("campaign_id", "cashback_campaigns.id"),
        _fk("order_id", "orders.id"),
        _fk("buyer_user_id", "users.id"),
        sa.Column("rule_snapshot", sa.JSON(), nullable=False),
        sa.Column("order_base_paise", sa.BigInteger(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=False),
        sa.Column("status", _enum("cashback_reward_status"), server_default="PENDING_REVIEW", nullable=False),
        sa.Column("created_from_settlement_id", sa.Uuid(), nullable=False),
        _fk("voided_by_user_id", "users.id", nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column("void_reason", sa.String(500)),
        sa.UniqueConstraint("campaign_id", "order_id", name="uq_cashback_reward_campaign_order"),
        sa.CheckConstraint("order_base_paise > 0", name="ck_cashback_reward_base_positive"),
        sa.CheckConstraint("amount_paise > 0", name="ck_cashback_reward_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_cashback_reward_currency_inr"),
        sa.CheckConstraint(
            "(status = 'PENDING_REVIEW' AND voided_at IS NULL) OR (status = 'VOIDED' AND voided_at IS NOT NULL)",
            name="ck_cashback_reward_status_timestamps",
        ),
    )
    _idx("cashback_rewards", "campaign_id", "order_id", "buyer_user_id", "status")

    op.create_table(
        "affiliates",
        *_base(),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        _fk("owner_user_id", "users.id", nullable=True),
        sa.Column("commission_type", _enum("affiliate_commission_type"), nullable=False),
        sa.Column("fixed_commission_paise", sa.BigInteger()),
        sa.Column("percentage_bps", sa.Integer()),
        sa.Column("max_commission_paise", sa.BigInteger()),
        sa.Column("minimum_order_paise", sa.BigInteger(), nullable=False),
        sa.Column("status", _enum("affiliate_status"), server_default="PENDING", nullable=False),
        _fk("created_by_user_id", "users.id"),
        _fk("activated_by_user_id", "users.id", nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("code", name="uq_affiliates_code"),
        sa.UniqueConstraint("owner_user_id", name="uq_affiliates_owner_user_id"),
        sa.CheckConstraint("code = upper(code)", name="ck_affiliate_code_uppercase"),
        sa.CheckConstraint("minimum_order_paise > 0", name="ck_affiliate_minimum_positive"),
        sa.CheckConstraint(
            "(commission_type = 'FIXED_PAISE' AND fixed_commission_paise IS NOT NULL AND fixed_commission_paise > 0 "
            "AND percentage_bps IS NULL AND max_commission_paise IS NULL) OR "
            "(commission_type = 'PERCENT_BPS' AND percentage_bps IS NOT NULL AND percentage_bps > 0 "
            "AND percentage_bps <= 10000 AND fixed_commission_paise IS NULL "
            "AND (max_commission_paise IS NULL OR max_commission_paise > 0))",
            name="ck_affiliate_commission_definition",
        ),
    )
    _idx("affiliates", "code", "status", "created_by_user_id")

    op.create_table(
        "affiliate_clicks",
        *_base(),
        _fk("affiliate_id", "affiliates.id"),
        sa.Column("click_reference", sa.String(128), nullable=False),
        sa.Column("landing_path", sa.String(255)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("click_reference", name="uq_affiliate_click_reference"),
    )
    _idx("affiliate_clicks", "affiliate_id", "occurred_at")

    op.create_table(
        "affiliate_conversions",
        *_base(),
        _fk("affiliate_id", "affiliates.id"),
        _fk("order_id", "orders.id"),
        _fk("buyer_user_id", "users.id"),
        sa.Column("affiliate_code_snapshot", sa.String(64), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("evidence_reference", sa.String(255), nullable=False),
        sa.Column("status", _enum("affiliate_conversion_status"), server_default="PENDING_REVIEW", nullable=False),
        sa.Column("created_from_settlement_id", sa.Uuid(), nullable=False),
        _fk("attributed_by_user_id", "users.id"),
        _fk("voided_by_user_id", "users.id", nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column("void_reason", sa.String(500)),
        sa.UniqueConstraint("order_id", name="uq_affiliate_conversion_order"),
        sa.CheckConstraint(
            "(status = 'PENDING_REVIEW' AND voided_at IS NULL) OR (status = 'VOIDED' AND voided_at IS NOT NULL)",
            name="ck_affiliate_conversion_status_timestamps",
        ),
    )
    _idx("affiliate_conversions", "affiliate_id", "order_id", "buyer_user_id", "status")

    op.create_table(
        "affiliate_commissions",
        *_base(),
        _fk("conversion_id", "affiliate_conversions.id"),
        _fk("affiliate_id", "affiliates.id"),
        _fk("beneficiary_user_id", "users.id", nullable=True),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=False),
        sa.Column("status", _enum("affiliate_commission_status"), server_default="PENDING_REVIEW", nullable=False),
        _fk("voided_by_user_id", "users.id", nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column("void_reason", sa.String(500)),
        sa.UniqueConstraint("conversion_id", name="uq_affiliate_commission_conversion"),
        sa.CheckConstraint("amount_paise > 0", name="ck_affiliate_commission_amount_positive"),
        sa.CheckConstraint("currency = 'INR'", name="ck_affiliate_commission_currency_inr"),
        sa.CheckConstraint(
            "(status = 'PENDING_REVIEW' AND voided_at IS NULL) OR (status = 'VOIDED' AND voided_at IS NOT NULL)",
            name="ck_affiliate_commission_status_timestamps",
        ),
    )
    _idx("affiliate_commissions", "affiliate_id", "status")


def downgrade() -> None:
    for name in (
        "affiliate_commissions", "affiliate_conversions", "affiliate_clicks", "affiliates",
        "cashback_rewards", "cashback_campaigns", "referral_rewards", "referrals",
        "referral_programs", "referral_profiles",
    ):
        op.drop_table(name)
    if op.get_bind().dialect.name == "postgresql":
        for name, values in reversed(tuple(ENUMS.items())):
            postgresql.ENUM(*values, name=name).drop(op.get_bind(), checkfirst=True)
