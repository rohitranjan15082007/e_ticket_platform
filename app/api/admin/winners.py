"""Administrator endpoints for the ordered, auditable draw workflow."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, status

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.user import User
from app.schemas.result import DrawAdminResponse, DrawCommitmentRequest, DrawRevealRequest
from app.services.winner_service import WinnerService


router = APIRouter(prefix="/admin/ticket-series", tags=["admin winners"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


@router.get("/{series_id}/draw", response_model=DrawAdminResponse)
async def get_draw(
    series_id: UUID, session: SessionDependency, current_user: AdminUser
) -> DrawAdminResponse:
    response = await WinnerService(session).admin_snapshot(
        series_id=series_id, actor_user_id=current_user.id
    )
    return DrawAdminResponse.model_validate(response)


@router.post("/{series_id}/draw/commit", response_model=DrawAdminResponse, status_code=status.HTTP_201_CREATED)
async def commit_draw_seed(
    series_id: UUID,
    payload: DrawCommitmentRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> DrawAdminResponse:
    result = await WinnerService(session).commit_seed(
        series_id=series_id,
        actor_user_id=current_user.id,
        seed_commitment=payload.seed_commitment,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return DrawAdminResponse.model_validate(result.response_payload)


@router.post("/{series_id}/draw/run", response_model=DrawAdminResponse)
async def run_draw(
    series_id: UUID,
    payload: DrawRevealRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> DrawAdminResponse:
    result = await WinnerService(session).run_draw(
        series_id=series_id,
        actor_user_id=current_user.id,
        seed_reveal=payload.seed_reveal,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return DrawAdminResponse.model_validate(result.response_payload)


@router.post("/{series_id}/draw/post-prizes", response_model=DrawAdminResponse)
async def post_draw_prizes(
    series_id: UUID,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> DrawAdminResponse:
    result = await WinnerService(session).post_prizes(
        series_id=series_id,
        actor_user_id=current_user.id,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return DrawAdminResponse.model_validate(result.response_payload)


@router.post("/{series_id}/draw/publish", response_model=DrawAdminResponse)
async def publish_draw_result(
    series_id: UUID,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> DrawAdminResponse:
    result = await WinnerService(session).publish_results(
        series_id=series_id,
        actor_user_id=current_user.id,
        idempotency_key=idempotency_key,
        commit=True,
    )
    return DrawAdminResponse.model_validate(result.response_payload)
