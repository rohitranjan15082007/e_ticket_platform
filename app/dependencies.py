"""FastAPI dependencies for authenticated identities and role checks."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName, has_any_role
from app.core.security import decode_access_token
from app.database import get_session
from app.exceptions import AuthenticationError, AuthorizationError
from app.models.user import User
from app.repositories.user_repository import get_user_by_id

SessionDependency = Annotated[AsyncSession, Depends(get_session)]
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: SessionDependency,
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Bearer authentication is required")
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = UUID(payload["sub"])
    except ValueError as error:
        raise AuthenticationError("Invalid access token subject") from error
    user = await get_user_by_id(session, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("User account is unavailable")
    return user


def require_roles(*roles: RoleName) -> Callable[..., User]:
    """Create a dependency that checks persisted roles rather than client claims."""

    async def role_dependency(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        assigned = {role.name for role in current_user.roles}
        if not has_any_role(assigned, set(roles)):
            raise AuthorizationError()
        return current_user

    return role_dependency
