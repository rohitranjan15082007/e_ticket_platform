"""Configured P2P eligibility gate used before funds are exposed or held."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.exceptions import AuthorizationError, ConflictError
from app.models.user import User


class P2PRiskService:
    """Evaluate only configured, auditable platform eligibility facts.

    The platform does not invent device/IP scoring in this project.  A host can
    enable the verified-account gate now and extend this service with an
    approved risk provider later; an absent provider never becomes a fake
    approval.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def assert_eligible(self, user_id: UUID, *, for_update: bool = False) -> User:
        statement = select(User).options(selectinload(User.roles)).where(User.id == user_id)
        if for_update:
            statement = statement.with_for_update()
        user = await self.session.scalar(statement)
        if user is None or not user.is_active:
            raise AuthorizationError("An active user account is required for P2P")
        if get_settings().p2p_require_verified_users and not user.is_verified:
            raise ConflictError(
                "P2P_RISK_REVIEW_REQUIRED",
                "Configured P2P risk policy requires a verified user account",
            )
        return user
