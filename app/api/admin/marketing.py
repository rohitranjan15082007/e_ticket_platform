"""Administrator-only marketing policy and pending reward review endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.user import User
from app.schemas.marketing import (
    AffiliateCommissionResponse, AffiliateConversionCreateRequest, AffiliateConversionResponse,
    AffiliateCreateRequest, AffiliateResponse, CashbackCampaignCreateRequest,
    CashbackCampaignResponse, CashbackRewardResponse, CouponCreateRequest,
    CouponDeactivateRequest, CouponResponse, MarketingReviewRequest, ReferralProgramCreateRequest,
    ReferralProgramResponse, ReferralRewardResponse,
)
from app.services.affiliate_service import AffiliateService
from app.services.cashback_service import CashbackService
from app.services.coupon_service import CouponService
from app.services.referral_service import ReferralService


router = APIRouter(prefix="/admin/marketing", tags=["admin marketing"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


@router.post("/coupons", response_model=CouponResponse, status_code=status.HTTP_201_CREATED)
async def create_coupon(
    payload: CouponCreateRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> CouponResponse:
    result = await CouponService(session).create(
        actor_user_id=current_user.id,
        code=payload.code,
        description=payload.description,
        discount_type=payload.discount_type,
        fixed_discount_paise=payload.fixed_discount_paise,
        percentage_bps=payload.percentage_bps,
        max_discount_paise=payload.max_discount_paise,
        minimum_order_paise=payload.minimum_order_paise,
        usage_limit=payload.usage_limit,
        per_user_limit=payload.per_user_limit,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return CouponResponse.model_validate(result.response_payload)


@router.get("/coupons", response_model=list[CouponResponse])
async def list_coupons(
    session: SessionDependency,
    current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> list[CouponResponse]:
    coupons = await CouponService(session).list_for_admin(limit=limit)
    return [CouponResponse.model_validate(CouponService.snapshot(coupon)) for coupon in coupons]


@router.post("/coupons/{coupon_id}/deactivate", response_model=CouponResponse)
async def deactivate_coupon(
    coupon_id: UUID,
    payload: CouponDeactivateRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> CouponResponse:
    result = await CouponService(session).deactivate(
        coupon_id=coupon_id,
        actor_user_id=current_user.id,
        reason=payload.reason,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return CouponResponse.model_validate(result.response_payload)


@router.post("/referral-programs", response_model=ReferralProgramResponse, status_code=status.HTTP_201_CREATED)
async def create_referral_program(
    payload: ReferralProgramCreateRequest, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> ReferralProgramResponse:
    result = await ReferralService(session).create_program(
        actor_user_id=current_user.id, name=payload.name, description=payload.description,
        referrer_reward_paise=payload.referrer_reward_paise,
        referred_reward_paise=payload.referred_reward_paise,
        minimum_order_paise=payload.minimum_order_paise,
        starts_at=payload.starts_at, ends_at=payload.ends_at,
        idempotency_key=idempotency_key, commit=True,
    )
    return ReferralProgramResponse.model_validate(result.response_payload)


@router.get("/referral-programs", response_model=list[ReferralProgramResponse])
async def list_referral_programs(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> list[ReferralProgramResponse]:
    service = ReferralService(session)
    return [
        ReferralProgramResponse.model_validate(service.program_snapshot(program))
        for program in await service.list_programs(limit=limit)
    ]


@router.post("/referral-programs/{program_id}/activate", response_model=ReferralProgramResponse)
async def activate_referral_program(
    program_id: UUID, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> ReferralProgramResponse:
    result = await ReferralService(session).activate_program(
        program_id=program_id, actor_user_id=current_user.id,
        idempotency_key=idempotency_key, commit=True,
    )
    return ReferralProgramResponse.model_validate(result.response_payload)


@router.post("/referral-programs/{program_id}/disable", response_model=ReferralProgramResponse)
async def disable_referral_program(
    program_id: UUID, payload: MarketingReviewRequest, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> ReferralProgramResponse:
    result = await ReferralService(session).disable_program(
        program_id=program_id, actor_user_id=current_user.id, reason=payload.reason,
        idempotency_key=idempotency_key, commit=True,
    )
    return ReferralProgramResponse.model_validate(result.response_payload)


@router.get("/referral-rewards", response_model=list[ReferralRewardResponse])
async def list_referral_rewards(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> list[ReferralRewardResponse]:
    service = ReferralService(session)
    return [
        ReferralRewardResponse.model_validate(service.reward_snapshot(reward))
        for reward in await service.list_rewards(limit=limit)
    ]


@router.post("/referral-rewards/{reward_id}/void", response_model=ReferralRewardResponse)
async def void_referral_reward(
    reward_id: UUID, payload: MarketingReviewRequest, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> ReferralRewardResponse:
    response = await ReferralService(session).void_reward(
        reward_id=reward_id, actor_user_id=current_user.id, reason=payload.reason,
        idempotency_key=idempotency_key, commit=True,
    )
    return ReferralRewardResponse.model_validate(response)


@router.post("/cashback-campaigns", response_model=CashbackCampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_cashback_campaign(
    payload: CashbackCampaignCreateRequest, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> CashbackCampaignResponse:
    _, response, _ = await CashbackService(session).create_campaign(
        actor_user_id=current_user.id, code=payload.code, name=payload.name,
        description=payload.description, reward_type=payload.reward_type,
        fixed_reward_paise=payload.fixed_reward_paise,
        percentage_bps=payload.percentage_bps, max_reward_paise=payload.max_reward_paise,
        minimum_order_paise=payload.minimum_order_paise,
        starts_at=payload.starts_at, ends_at=payload.ends_at,
        idempotency_key=idempotency_key, commit=True,
    )
    return CashbackCampaignResponse.model_validate(response)


@router.get("/cashback-campaigns", response_model=list[CashbackCampaignResponse])
async def list_cashback_campaigns(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> list[CashbackCampaignResponse]:
    service = CashbackService(session)
    return [
        CashbackCampaignResponse.model_validate(service.snapshot(campaign))
        for campaign in await service.list_campaigns(limit=limit)
    ]


@router.post("/cashback-campaigns/{campaign_id}/activate", response_model=CashbackCampaignResponse)
async def activate_cashback_campaign(
    campaign_id: UUID, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> CashbackCampaignResponse:
    _, response, _ = await CashbackService(session).activate_campaign(
        campaign_id=campaign_id, actor_user_id=current_user.id,
        idempotency_key=idempotency_key, commit=True,
    )
    return CashbackCampaignResponse.model_validate(response)


@router.post("/cashback-campaigns/{campaign_id}/disable", response_model=CashbackCampaignResponse)
async def disable_cashback_campaign(
    campaign_id: UUID, payload: MarketingReviewRequest, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> CashbackCampaignResponse:
    response = await CashbackService(session).disable_campaign(
        campaign_id=campaign_id, actor_user_id=current_user.id, reason=payload.reason,
        idempotency_key=idempotency_key, commit=True,
    )
    return CashbackCampaignResponse.model_validate(response)


@router.get("/cashback-rewards", response_model=list[CashbackRewardResponse])
async def list_cashback_rewards(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> list[CashbackRewardResponse]:
    service = CashbackService(session)
    return [
        CashbackRewardResponse.model_validate(service.reward_snapshot(reward))
        for reward in await service.list_rewards(limit=limit)
    ]


@router.post("/cashback-rewards/{reward_id}/void", response_model=CashbackRewardResponse)
async def void_cashback_reward(
    reward_id: UUID, payload: MarketingReviewRequest, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> CashbackRewardResponse:
    response = await CashbackService(session).void_reward(
        reward_id=reward_id, actor_user_id=current_user.id, reason=payload.reason,
        idempotency_key=idempotency_key, commit=True,
    )
    return CashbackRewardResponse.model_validate(response)


@router.post("/affiliates", response_model=AffiliateResponse, status_code=status.HTTP_201_CREATED)
async def create_affiliate(
    payload: AffiliateCreateRequest, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> AffiliateResponse:
    _, response, _ = await AffiliateService(session).create(
        actor_user_id=current_user.id, code=payload.code, display_name=payload.display_name,
        owner_user_id=payload.owner_user_id, commission_type=payload.commission_type,
        fixed_commission_paise=payload.fixed_commission_paise,
        percentage_bps=payload.percentage_bps,
        max_commission_paise=payload.max_commission_paise,
        minimum_order_paise=payload.minimum_order_paise,
        idempotency_key=idempotency_key, commit=True,
    )
    return AffiliateResponse.model_validate(response)


@router.get("/affiliates", response_model=list[AffiliateResponse])
async def list_affiliates(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> list[AffiliateResponse]:
    service = AffiliateService(session)
    return [
        AffiliateResponse.model_validate(service.snapshot(affiliate))
        for affiliate in await service.list_affiliates(limit=limit)
    ]


@router.post("/affiliates/{affiliate_id}/activate", response_model=AffiliateResponse)
async def activate_affiliate(
    affiliate_id: UUID, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> AffiliateResponse:
    _, response, _ = await AffiliateService(session).activate(
        affiliate_id=affiliate_id, actor_user_id=current_user.id,
        idempotency_key=idempotency_key, commit=True,
    )
    return AffiliateResponse.model_validate(response)


@router.post("/affiliates/{affiliate_id}/suspend", response_model=AffiliateResponse)
async def suspend_affiliate(
    affiliate_id: UUID, payload: MarketingReviewRequest, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> AffiliateResponse:
    response = await AffiliateService(session).suspend(
        affiliate_id=affiliate_id, actor_user_id=current_user.id, reason=payload.reason,
        idempotency_key=idempotency_key, commit=True,
    )
    return AffiliateResponse.model_validate(response)


@router.post("/affiliate-conversions", response_model=AffiliateConversionResponse, status_code=status.HTTP_201_CREATED)
async def create_affiliate_conversion(
    payload: AffiliateConversionCreateRequest, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> AffiliateConversionResponse:
    _, _, response, _ = await AffiliateService(session).attribute_settled_order(
        affiliate_id=payload.affiliate_id, order_id=payload.order_id,
        evidence_reference=payload.evidence_reference,
        actor_user_id=current_user.id, idempotency_key=idempotency_key, commit=True,
    )
    return AffiliateConversionResponse.model_validate(response)


@router.get("/affiliate-conversions", response_model=list[AffiliateConversionResponse])
async def list_affiliate_conversions(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> list[AffiliateConversionResponse]:
    service = AffiliateService(session)
    return [
        AffiliateConversionResponse.model_validate(service.conversion_snapshot(conversion))
        for conversion in await service.list_conversions(limit=limit)
    ]


@router.post("/affiliate-conversions/{conversion_id}/void", response_model=AffiliateConversionResponse)
async def void_affiliate_conversion(
    conversion_id: UUID, payload: MarketingReviewRequest, session: SessionDependency,
    current_user: AdminUser, idempotency_key: IdempotencyKey,
) -> AffiliateConversionResponse:
    response = await AffiliateService(session).void_conversion(
        conversion_id=conversion_id, actor_user_id=current_user.id, reason=payload.reason,
        idempotency_key=idempotency_key, commit=True,
    )
    return AffiliateConversionResponse.model_validate(response)


@router.get("/affiliate-commissions", response_model=list[AffiliateCommissionResponse])
async def list_affiliate_commissions(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=1_000),
) -> list[AffiliateCommissionResponse]:
    service = AffiliateService(session)
    return [
        AffiliateCommissionResponse.model_validate(service.commission_snapshot(commission))
        for commission in await service.list_commissions(limit=limit)
    ]
