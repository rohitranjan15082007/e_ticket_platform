"""Admin-only audit metadata; financial state snapshots stay off this API."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.core.pagination import PageWindow
from app.core.permissions import RoleName
from app.dependencies import SessionDependency, require_roles
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.audit import AuditEventResponse


router = APIRouter(prefix="/admin/audit", tags=["admin audit"])
AdminUser = Annotated[User, Depends(require_roles(RoleName.ADMIN))]


@router.get("", response_model=list[AuditEventResponse])
async def list_audit_events(
    session: SessionDependency,
    current_user: AdminUser,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    actor_user_id: UUID | None = None,
    entity_id: UUID | None = None,
    entity_type: str | None = Query(default=None, min_length=1, max_length=100),
    action: str | None = Query(default=None, min_length=1, max_length=100),
) -> list[AuditEventResponse]:
    # Project only safe metadata; never load snapshot or reason fields into this request.
    statement = select(
        AuditLog.id,
        AuditLog.actor_user_id,
        AuditLog.entity_type,
        AuditLog.entity_id,
        AuditLog.action,
        AuditLog.created_at,
    )
    if actor_user_id is not None:
        statement = statement.where(AuditLog.actor_user_id == actor_user_id)
    if entity_id is not None:
        statement = statement.where(AuditLog.entity_id == entity_id)
    if entity_type is not None:
        statement = statement.where(AuditLog.entity_type == entity_type)
    if action is not None:
        statement = statement.where(AuditLog.action == action)
    statement = PageWindow(limit, offset).apply(
        statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    rows = await session.execute(statement)
    return [AuditEventResponse.model_validate(row._mapping) for row in rows]
