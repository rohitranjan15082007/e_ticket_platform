"""Administrator-only ticket-series catalog endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.exceptions import ValidationError
from app.models.user import User
from app.models.ticket_series import TicketSeries
from app.schemas.ticket_series import (
    TicketSeriesCreateRequest,
    TicketSeriesResponse,
    TicketSeriesUpdateRequest,
)
from app.services.ticket_series_service import PrizeDraft, TicketSeriesService
from app.services.winner_service import WinnerService


router = APIRouter(prefix="/admin/ticket-series", tags=["admin ticket series"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


@router.get("", response_model=list[TicketSeriesResponse])
async def list_admin_series(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[TicketSeriesResponse]:
    rows = await session.scalars(
        select(TicketSeries).options(selectinload(TicketSeries.prizes))
        .order_by(TicketSeries.created_at.desc(), TicketSeries.id.desc()).limit(limit)
    )
    return [TicketSeriesResponse.model_validate(TicketSeriesService.snapshot(row)) for row in rows]


@router.get("/{series_id}", response_model=TicketSeriesResponse)
async def get_admin_series(
    series_id: UUID, session: SessionDependency, current_user: AdminUser,
) -> TicketSeriesResponse:
    row = await session.scalar(
        select(TicketSeries).options(selectinload(TicketSeries.prizes))
        .where(TicketSeries.id == series_id)
    )
    if row is None:
        raise ValidationError("UNKNOWN_SERIES", "Ticket series does not exist")
    return TicketSeriesResponse.model_validate(TicketSeriesService.snapshot(row))


def _prizes(payload: TicketSeriesCreateRequest | TicketSeriesUpdateRequest) -> list[PrizeDraft] | None:
    if payload.prizes is None:
        return None
    return [PrizeDraft(value.rank, value.title, value.prize_paise) for value in payload.prizes]


@router.post("", response_model=TicketSeriesResponse, status_code=status.HTTP_201_CREATED)
async def create_series(
    payload: TicketSeriesCreateRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> TicketSeriesResponse:
    result = await TicketSeriesService(session).create(
        actor_user_id=current_user.id,
        name=payload.name,
        description=payload.description,
        price_paise=payload.price_paise,
        ticket_limit=payload.ticket_limit,
        sales_start_at=payload.sales_start_at,
        sales_end_at=payload.sales_end_at,
        draw_at=payload.draw_at,
        prizes=_prizes(payload) or [],
        idempotency_key=idempotency_key,
        commit=True,
    )
    return TicketSeriesResponse.model_validate(result.response_payload)


@router.put("/{series_id}", response_model=TicketSeriesResponse)
async def update_series(
    series_id: UUID,
    payload: TicketSeriesUpdateRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> TicketSeriesResponse:
    description: object = payload.description if "description" in payload.model_fields_set else ...
    result = await TicketSeriesService(session).update(
        series_id=series_id,
        actor_user_id=current_user.id,
        name=payload.name,
        description=description,
        price_paise=payload.price_paise,
        ticket_limit=payload.ticket_limit,
        sales_start_at=payload.sales_start_at,
        sales_end_at=payload.sales_end_at,
        draw_at=payload.draw_at,
        prizes=_prizes(payload),
        idempotency_key=idempotency_key,
        commit=True,
    )
    return TicketSeriesResponse.model_validate(result.response_payload)


@router.post("/{series_id}/publish", response_model=TicketSeriesResponse)
async def publish_series(
    series_id: UUID, session: SessionDependency, current_user: AdminUser, idempotency_key: IdempotencyKey
) -> TicketSeriesResponse:
    result = await TicketSeriesService(session).publish(
        series_id=series_id, actor_user_id=current_user.id, idempotency_key=idempotency_key, commit=True
    )
    return TicketSeriesResponse.model_validate(result.response_payload)


@router.post("/{series_id}/open", response_model=TicketSeriesResponse)
async def open_series(
    series_id: UUID, session: SessionDependency, current_user: AdminUser, idempotency_key: IdempotencyKey
) -> TicketSeriesResponse:
    result = await TicketSeriesService(session).open(
        series_id=series_id, actor_user_id=current_user.id, idempotency_key=idempotency_key, commit=True
    )
    return TicketSeriesResponse.model_validate(result.response_payload)


@router.post("/{series_id}/close", response_model=TicketSeriesResponse)
async def close_series(
    series_id: UUID, session: SessionDependency, current_user: AdminUser, idempotency_key: IdempotencyKey
) -> TicketSeriesResponse:
    result = await WinnerService(session).close_sales(
        series_id=series_id, actor_user_id=current_user.id, idempotency_key=idempotency_key, commit=True
    )
    series_snapshot = result.response_payload.get("series")
    if not isinstance(series_snapshot, dict):
        raise RuntimeError("Draw close response is missing its ticket-series snapshot")
    return TicketSeriesResponse.model_validate(series_snapshot)


@router.post("/{series_id}/cancel", response_model=TicketSeriesResponse)
async def cancel_series(
    series_id: UUID, session: SessionDependency, current_user: AdminUser, idempotency_key: IdempotencyKey
) -> TicketSeriesResponse:
    result = await TicketSeriesService(session).cancel(
        series_id=series_id, actor_user_id=current_user.id, idempotency_key=idempotency_key, commit=True
    )
    return TicketSeriesResponse.model_validate(result.response_payload)
