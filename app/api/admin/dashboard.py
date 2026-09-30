"""Administrator-only operational summary."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.user import User
from app.schemas.admin import AdminDashboardResponse
from app.services.dashboard_service import DashboardService


router = APIRouter(prefix="/admin/dashboard", tags=["admin dashboard"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]


@router.get("/summary", response_model=AdminDashboardResponse)
async def get_dashboard_summary(
    session: SessionDependency, current_user: AdminUser,
) -> AdminDashboardResponse:
    return AdminDashboardResponse.model_validate(await DashboardService(session).summary())
