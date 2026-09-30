"""Authenticated in-app notification reads without payload exposure."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies import SessionDependency, get_current_user
from app.models.user import User
from app.schemas.notification_read import NotificationReadResponse
from app.services.notification_read_service import NotificationReadService


router = APIRouter(prefix="/notifications", tags=["notifications"])
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("", response_model=list[NotificationReadResponse])
async def list_notifications(
    session: SessionDependency,
    current_user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[NotificationReadResponse]:
    rows = await NotificationReadService(session).list_for_user(
        user_id=current_user.id, limit=limit,
    )
    return [NotificationReadResponse.model_validate(row) for row in rows]
