"""Authenticated referral profile and claim endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, status

from app.dependencies import SessionDependency, get_current_user
from app.models.user import User
from app.schemas.marketing import ReferralClaimRequest, ReferralProfileResponse, ReferralResponse
from app.services.referral_service import ReferralService


router = APIRouter(prefix="/referrals", tags=["referrals"])
CurrentUser = Annotated[User, Depends(get_current_user)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


@router.post("/profile", response_model=ReferralProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_profile(
    session: SessionDependency, current_user: CurrentUser, idempotency_key: IdempotencyKey,
) -> ReferralProfileResponse:
    result = await ReferralService(session).create_profile(
        user_id=current_user.id, idempotency_key=idempotency_key, commit=True
    )
    return ReferralProfileResponse.model_validate(result.response_payload)


@router.get("/profile", response_model=ReferralProfileResponse | None)
async def get_profile(session: SessionDependency, current_user: CurrentUser) -> ReferralProfileResponse | None:
    service = ReferralService(session)
    profile = await service.get_profile_for_user(user_id=current_user.id)
    return ReferralProfileResponse.model_validate(service.profile_snapshot(profile)) if profile else None


@router.post("/claim", response_model=ReferralResponse, status_code=status.HTTP_201_CREATED)
async def claim_referral(
    payload: ReferralClaimRequest, session: SessionDependency,
    current_user: CurrentUser, idempotency_key: IdempotencyKey,
) -> ReferralResponse:
    result = await ReferralService(session).claim(
        referred_user_id=current_user.id, referral_code=payload.referral_code,
        idempotency_key=idempotency_key, commit=True,
    )
    return ReferralResponse.model_validate(result.response_payload)


@router.get("/claim", response_model=ReferralResponse | None)
async def get_claim(session: SessionDependency, current_user: CurrentUser) -> ReferralResponse | None:
    service = ReferralService(session)
    referral = await service.get_referral_for_user(user_id=current_user.id)
    return ReferralResponse.model_validate(service.referral_snapshot(referral)) if referral else None
