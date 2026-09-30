"""Bounded, masked administrator reads without account mutation."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.pagination import PageWindow
from app.exceptions import ValidationError
from app.models.user import User


def email_hint(value: str) -> str:
    local, _, domain = value.partition("@")
    return f"{local[:2]}***@{domain}"


class AdminUserReadService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def snapshot(user: User) -> dict[str, object]:
        return {
            "id": user.id,
            "email_hint": email_hint(user.email),
            "full_name": user.full_name,
            "is_active": user.is_active,
            "is_verified": user.is_verified,
            "roles": sorted(role.name for role in user.roles),
            "created_at": user.created_at,
        }

    async def list_users(self, *, limit: int, offset: int) -> list[dict[str, object]]:
        statement = select(User).options(selectinload(User.roles)).order_by(
            User.created_at.desc(), User.id.desc()
        )
        users = (await self.session.scalars(PageWindow(limit, offset).apply(statement))).all()
        return [self.snapshot(user) for user in users]

    async def get_user(self, *, user_id: UUID) -> dict[str, object]:
        user = await self.session.scalar(
            select(User).options(selectinload(User.roles)).where(User.id == user_id)
        )
        if user is None:
            raise ValidationError("UNKNOWN_USER", "User does not exist")
        return self.snapshot(user)
