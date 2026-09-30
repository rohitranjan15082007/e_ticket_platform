"""Authenticated read-only profile endpoint.

Profile changes are not exposed until an audited, idempotent edit policy is defined.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.user import ProfileResponse


router = APIRouter(prefix="/profile", tags=["profile"])
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("", response_model=ProfileResponse)
async def get_profile(current_user: CurrentUser) -> ProfileResponse:
    return ProfileResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        is_active=current_user.is_active,
        is_verified=current_user.is_verified,
        roles=sorted(role.name for role in current_user.roles),
        created_at=current_user.created_at,
    )
