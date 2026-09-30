"""Administrator-only multi-series package catalog endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.user import User
from app.repositories.ticket_repository import get_ticket_package, list_admin_ticket_packages
from app.exceptions import ValidationError
from app.schemas.ticket_package import (
    TicketPackageCreateRequest,
    TicketPackageResponse,
    TicketPackageUpdateRequest,
)
from app.services.ticket_package_service import PackageItemDraft, TicketPackageService


router = APIRouter(prefix="/admin/ticket-packages", tags=["admin ticket packages"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=255)]


def _items(payload: TicketPackageCreateRequest | TicketPackageUpdateRequest) -> list[PackageItemDraft] | None:
    if payload.items is None:
        return None
    return [PackageItemDraft(item.series_id, item.quantity) for item in payload.items]


@router.get("", response_model=list[TicketPackageResponse])
async def list_admin_packages(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[TicketPackageResponse]:
    rows = await list_admin_ticket_packages(session, limit=limit)
    return [TicketPackageResponse.model_validate(TicketPackageService.snapshot(row)) for row in rows]


@router.get("/{package_id}", response_model=TicketPackageResponse)
async def get_admin_package(
    package_id: UUID, session: SessionDependency, current_user: AdminUser,
) -> TicketPackageResponse:
    row = await get_ticket_package(session, package_id)
    if row is None:
        raise ValidationError("UNKNOWN_PACKAGE", "Ticket package does not exist")
    return TicketPackageResponse.model_validate(TicketPackageService.snapshot(row))


@router.post("", response_model=TicketPackageResponse, status_code=status.HTTP_201_CREATED)
async def create_package(
    payload: TicketPackageCreateRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> TicketPackageResponse:
    result = await TicketPackageService(session).create(
        actor_user_id=current_user.id,
        name=payload.name,
        description=payload.description,
        price_paise=payload.price_paise,
        inventory_limit=payload.inventory_limit,
        items=_items(payload) or [],
        idempotency_key=idempotency_key,
        commit=True,
    )
    return TicketPackageResponse.model_validate(result.response_payload)


@router.put("/{package_id}", response_model=TicketPackageResponse)
async def update_package(
    package_id: UUID,
    payload: TicketPackageUpdateRequest,
    session: SessionDependency,
    current_user: AdminUser,
    idempotency_key: IdempotencyKey,
) -> TicketPackageResponse:
    description: object = payload.description if "description" in payload.model_fields_set else ...
    inventory_limit: object = payload.inventory_limit if "inventory_limit" in payload.model_fields_set else ...
    result = await TicketPackageService(session).update(
        package_id=package_id,
        actor_user_id=current_user.id,
        name=payload.name,
        description=description,
        price_paise=payload.price_paise,
        inventory_limit=inventory_limit,
        items=_items(payload),
        idempotency_key=idempotency_key,
        commit=True,
    )
    return TicketPackageResponse.model_validate(result.response_payload)


@router.post("/{package_id}/deactivate", response_model=TicketPackageResponse)
async def deactivate_package(
    package_id: UUID, session: SessionDependency, current_user: AdminUser, idempotency_key: IdempotencyKey
) -> TicketPackageResponse:
    result = await TicketPackageService(session).deactivate(
        package_id=package_id, actor_user_id=current_user.id, idempotency_key=idempotency_key, commit=True
    )
    return TicketPackageResponse.model_validate(result.response_payload)
