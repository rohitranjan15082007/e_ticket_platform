"""Authenticated P2P withdrawal request and safe-cancellation endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy import select

from app.dependencies import SessionDependency, get_current_user
from app.models.user import User
from app.models.withdrawal import PaymentDestination, PaymentDestinationStatus, WithdrawalRequest
from app.schemas.withdrawal import WithdrawalCreateRequest, WithdrawalResponse, VerifiedDestinationResponse
from app.services.withdrawal_service import WithdrawalService


router = APIRouter(prefix="/withdrawals", tags=["withdrawals"])
CurrentUser = Annotated[User, Depends(get_current_user)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


@router.post("", response_model=WithdrawalResponse, status_code=status.HTTP_201_CREATED)
async def create_withdrawal(
    payload: WithdrawalCreateRequest,
    session: SessionDependency,
    current_user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> WithdrawalResponse:
    result = await WithdrawalService(session).create(
        actor_user_id=current_user.id,
        amount_paise=payload.amount_paise,
        payment_destination_id=payload.payment_destination_id,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return WithdrawalResponse.model_validate(result.response_payload)


@router.get("", response_model=list[WithdrawalResponse])
async def list_my_withdrawals(
    session: SessionDependency, current_user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[WithdrawalResponse]:
    rows = await session.scalars(
        select(WithdrawalRequest).where(WithdrawalRequest.user_id == current_user.id)
        .order_by(WithdrawalRequest.created_at.desc(), WithdrawalRequest.id.desc()).limit(limit)
    )
    return [WithdrawalResponse.model_validate(WithdrawalService.snapshot(row)) for row in rows]


@router.get("/destinations", response_model=list[VerifiedDestinationResponse])
async def list_my_verified_destinations(
    session: SessionDependency, current_user: CurrentUser,
) -> list[VerifiedDestinationResponse]:
    rows = await session.scalars(
        select(PaymentDestination).where(
            PaymentDestination.user_id == current_user.id,
            PaymentDestination.status == PaymentDestinationStatus.VERIFIED,
        ).order_by(PaymentDestination.display_label, PaymentDestination.id).limit(100)
    )
    return [
        VerifiedDestinationResponse(
            id=row.id, provider_namespace=row.provider_namespace,
            display_label=row.display_label, verified_at=row.verified_at,
        )
        for row in rows
    ]


@router.get("/{withdrawal_id}", response_model=WithdrawalResponse)
async def get_withdrawal(
    withdrawal_id: UUID, session: SessionDependency, current_user: CurrentUser
) -> WithdrawalResponse:
    withdrawal = await WithdrawalService(session).get_for_actor(
        withdrawal_id=withdrawal_id, actor_user_id=current_user.id
    )
    return WithdrawalResponse.model_validate(WithdrawalService.snapshot(withdrawal))


@router.post("/{withdrawal_id}/cancel", response_model=WithdrawalResponse)
async def cancel_withdrawal(
    withdrawal_id: UUID,
    session: SessionDependency,
    current_user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> WithdrawalResponse:
    result = await WithdrawalService(session).cancel(
        withdrawal_id=withdrawal_id,
        actor_user_id=current_user.id,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return WithdrawalResponse.model_validate(result.response_payload)
