"""Administrator withdrawal review reads; decisions use the existing P2P workflow."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.user import User
from app.models.withdrawal import WithdrawalRequest, WithdrawalStatus
from app.schemas.withdrawal import WithdrawalResponse
from app.services.withdrawal_service import WithdrawalService


router = APIRouter(prefix="/admin/withdrawals", tags=["admin withdrawals"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]


@router.get("", response_model=list[WithdrawalResponse])
async def list_withdrawals_for_review(
    session: SessionDependency, current_user: AdminUser,
    limit: int = Query(default=100, ge=1, le=200),
    status: WithdrawalStatus | None = None,
) -> list[WithdrawalResponse]:
    statement = select(WithdrawalRequest)
    if status is not None:
        statement = statement.where(WithdrawalRequest.status == status)
    rows = await session.scalars(
        statement.order_by(WithdrawalRequest.created_at.desc(), WithdrawalRequest.id.desc()).limit(limit)
    )
    return [WithdrawalResponse.model_validate(WithdrawalService.snapshot(row)) for row in rows]


@router.get("/{withdrawal_id}", response_model=WithdrawalResponse)
async def get_withdrawal_for_review(
    withdrawal_id: UUID, session: SessionDependency, current_user: AdminUser,
) -> WithdrawalResponse:
    row = await WithdrawalService(session).get_for_actor(
        withdrawal_id=withdrawal_id, actor_user_id=current_user.id
    )
    return WithdrawalResponse.model_validate(WithdrawalService.snapshot(row))
