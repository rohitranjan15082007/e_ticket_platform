"""Administrator-only immutable revenue report endpoints."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.user import User
from app.schemas.marketing import RevenueAllocationResponse, RevenueReportResponse
from app.services.revenue_service import RevenueService


router = APIRouter(prefix="/admin/revenue", tags=["admin revenue"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]


@router.get("/report", response_model=RevenueReportResponse)
async def revenue_report(
    session: SessionDependency,
    current_user: AdminUser,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> RevenueReportResponse:
    report = await RevenueService(session).report(starts_at=starts_at, ends_at=ends_at)
    return RevenueReportResponse.model_validate(report)


@router.get("/allocations", response_model=list[RevenueAllocationResponse])
async def list_revenue_allocations(
    session: SessionDependency,
    current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=1_000),
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> list[RevenueAllocationResponse]:
    allocations = await RevenueService(session).list_allocations(
        limit=limit, starts_at=starts_at, ends_at=ends_at
    )
    return [
        RevenueAllocationResponse.model_validate(RevenueService.snapshot(allocation))
        for allocation in allocations
    ]
