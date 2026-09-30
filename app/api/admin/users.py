"""Administrator-only, bounded and masked user reads."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.user import User
from app.schemas.user import AdminUserSummaryResponse
from app.services.admin_user_read_service import AdminUserReadService


router = APIRouter(prefix="/admin/users", tags=["admin users"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]


@router.get("", response_model=list[AdminUserSummaryResponse])
async def list_admin_users(
    session: SessionDependency,
    current_user: AdminUser,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> list[AdminUserSummaryResponse]:
    rows = await AdminUserReadService(session).list_users(limit=limit, offset=offset)
    return [AdminUserSummaryResponse.model_validate(row) for row in rows]


@router.get("/{user_id}", response_model=AdminUserSummaryResponse)
async def get_admin_user(
    user_id: UUID, session: SessionDependency, current_user: AdminUser,
) -> AdminUserSummaryResponse:
    row = await AdminUserReadService(session).get_user(user_id=user_id)
    return AdminUserSummaryResponse.model_validate(row)
