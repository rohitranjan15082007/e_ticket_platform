"""Phase 7 marketing and revenue API contracts."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.coupon import CouponDiscountType
from app.models.affiliate import (
    AffiliateCommissionStatus, AffiliateCommissionType, AffiliateConversionStatus, AffiliateStatus,
)
from app.models.cashback import CashbackCampaignStatus, CashbackRewardStatus, CashbackRewardType
from app.models.referral import ReferralProgramStatus, ReferralRewardRecipient, ReferralRewardStatus, ReferralStatus
from app.models.revenue_allocation import RevenueAllocationSource


def _positive_paise(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer paise value")
    return value


def _nonnegative_paise(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer paise value")
    return value


class CouponCreateRequest(BaseModel):
    """Admin coupon definition; all discount arithmetic remains server-owned."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=3, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    discount_type: CouponDiscountType
    fixed_discount_paise: int | None = None
    percentage_bps: int | None = None
    max_discount_paise: int | None = None
    minimum_order_paise: int = 0
    usage_limit: int | None = None
    per_user_limit: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not value:
            raise ValueError("code cannot be blank")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("description cannot be blank")
        return value

    @field_validator("fixed_discount_paise", "max_discount_paise", mode="before")
    @classmethod
    def validate_optional_positive_paise(cls, value: object) -> int | None:
        if value is None:
            return None
        return _positive_paise(value, field="discount amount")

    @field_validator("minimum_order_paise", mode="before")
    @classmethod
    def validate_minimum(cls, value: object) -> int:
        return _nonnegative_paise(value, field="minimum_order_paise")

    @field_validator("percentage_bps")
    @classmethod
    def validate_percentage(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 10_000:
            raise ValueError("percentage_bps must be an integer between 1 and 10000")
        return value

    @field_validator("usage_limit", "per_user_limit")
    @classmethod
    def validate_optional_limit(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("usage limits must be positive integers")
        return value

    @model_validator(mode="after")
    def validate_definition(self) -> "CouponCreateRequest":
        for field, value in (("starts_at", self.starts_at), ("ends_at", self.ends_at)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field} must include a timezone")
        if self.starts_at is not None and self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.discount_type == CouponDiscountType.FIXED_PAISE:
            if (
                self.fixed_discount_paise is None
                or self.percentage_bps is not None
                or self.max_discount_paise is not None
            ):
                raise ValueError(
                    "FIXED_PAISE requires fixed_discount_paise and disallows percentage_bps/max_discount_paise"
                )
        elif self.fixed_discount_paise is not None or self.percentage_bps is None:
            raise ValueError("PERCENT_BPS requires percentage_bps and disallows fixed_discount_paise")
        return self


class CouponDeactivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason cannot be blank")
        return value


class CouponResponse(BaseModel):
    id: UUID
    code: str
    description: str | None
    discount_type: CouponDiscountType
    fixed_discount_paise: int | None
    percentage_bps: int | None
    max_discount_paise: int | None
    minimum_order_paise: int
    usage_limit: int | None
    per_user_limit: int | None
    active_redemption_count: int
    starts_at: datetime | None
    ends_at: datetime | None
    is_active: bool
    created_by_user_id: UUID


class RevenueAllocationResponse(BaseModel):
    id: UUID
    order_id: UUID
    settlement_reference_id: UUID
    source: RevenueAllocationSource
    source_account_id: UUID
    journal_group_id: UUID
    idempotency_record_id: UUID
    allocation_base_paise: int
    prize_pool_paise: int
    marketing_paise: int
    operations_paise: int
    reserve_paise: int
    profit_growth_paise: int
    currency: str
    allocated_by_user_id: UUID | None
    allocated_at: datetime


class RevenueReportResponse(BaseModel):
    starts_at: datetime | None
    ends_at: datetime | None
    currency: str
    allocation_count: int
    allocation_base_paise: int
    prize_pool_paise: int
    marketing_paise: int
    operations_paise: int
    reserve_paise: int
    profit_growth_paise: int


PositivePaise = Annotated[int, Field(strict=True, gt=0)]
OptionalPositivePaise = Annotated[int, Field(strict=True, gt=0)] | None
OptionalBps = Annotated[int, Field(strict=True, gt=0, le=10_000)] | None


class ReferralProgramCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    referrer_reward_paise: PositivePaise
    referred_reward_paise: PositivePaise
    minimum_order_paise: PositivePaise
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class ReferralProgramResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    referrer_reward_paise: int
    referred_reward_paise: int
    minimum_order_paise: int
    starts_at: datetime | None
    ends_at: datetime | None
    status: ReferralProgramStatus
    created_by_user_id: UUID
    activated_by_user_id: UUID | None
    activated_at: datetime | None


class ReferralProfileResponse(BaseModel):
    id: UUID
    user_id: UUID
    code: str
    is_active: bool


class ReferralClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    referral_code: str = Field(min_length=3, max_length=64)


class ReferralResponse(BaseModel):
    id: UUID
    program_id: UUID
    referrer_user_id: UUID
    referred_user_id: UUID
    referral_code_snapshot: str
    program_snapshot: dict[str, object]
    status: ReferralStatus
    qualifying_order_id: UUID | None
    claimed_at: datetime
    qualified_at: datetime | None
    voided_at: datetime | None
    void_reason: str | None


class ReferralRewardResponse(BaseModel):
    id: UUID
    referral_id: UUID
    qualifying_order_id: UUID
    recipient_user_id: UUID
    recipient: ReferralRewardRecipient
    amount_paise: int
    currency: str
    status: ReferralRewardStatus
    created_from_settlement_id: UUID
    voided_at: datetime | None
    void_reason: str | None


class CashbackCampaignCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    reward_type: CashbackRewardType
    fixed_reward_paise: OptionalPositivePaise = None
    percentage_bps: OptionalBps = None
    max_reward_paise: OptionalPositivePaise = None
    minimum_order_paise: PositivePaise
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class CashbackCampaignResponse(BaseModel):
    id: UUID
    code: str
    name: str
    description: str | None
    reward_type: CashbackRewardType
    fixed_reward_paise: int | None
    percentage_bps: int | None
    max_reward_paise: int | None
    minimum_order_paise: int
    starts_at: datetime | None
    ends_at: datetime | None
    status: CashbackCampaignStatus
    created_by_user_id: UUID
    activated_by_user_id: UUID | None
    activated_at: datetime | None


class CashbackRewardResponse(BaseModel):
    id: UUID
    campaign_id: UUID
    order_id: UUID
    buyer_user_id: UUID
    rule_snapshot: dict[str, object]
    order_base_paise: int
    amount_paise: int
    currency: str
    status: CashbackRewardStatus
    created_from_settlement_id: UUID
    voided_at: datetime | None
    void_reason: str | None


class AffiliateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    owner_user_id: UUID | None = None
    commission_type: AffiliateCommissionType
    fixed_commission_paise: OptionalPositivePaise = None
    percentage_bps: OptionalBps = None
    max_commission_paise: OptionalPositivePaise = None
    minimum_order_paise: PositivePaise


class AffiliateResponse(BaseModel):
    id: UUID
    code: str
    display_name: str
    owner_user_id: UUID | None
    commission_type: AffiliateCommissionType
    fixed_commission_paise: int | None
    percentage_bps: int | None
    max_commission_paise: int | None
    minimum_order_paise: int
    status: AffiliateStatus
    created_by_user_id: UUID
    activated_by_user_id: UUID | None
    activated_at: datetime | None


class AffiliateConversionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    affiliate_id: UUID
    order_id: UUID
    evidence_reference: str = Field(min_length=1, max_length=255)


class AffiliateCommissionResponse(BaseModel):
    id: UUID
    conversion_id: UUID
    affiliate_id: UUID
    beneficiary_user_id: UUID | None
    amount_paise: int
    currency: str
    status: AffiliateCommissionStatus
    voided_at: datetime | None
    void_reason: str | None


class AffiliateConversionResponse(BaseModel):
    id: UUID
    affiliate_id: UUID
    order_id: UUID
    buyer_user_id: UUID
    affiliate_code_snapshot: str
    policy_snapshot: dict[str, object]
    evidence_reference: str
    status: AffiliateConversionStatus
    created_from_settlement_id: UUID
    attributed_by_user_id: UUID
    voided_at: datetime | None
    void_reason: str | None
    commission: AffiliateCommissionResponse | None = None


class MarketingReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)
