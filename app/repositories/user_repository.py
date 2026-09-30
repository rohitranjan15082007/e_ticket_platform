"""Identity and role queries with no business decisions."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import Role, User


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(
        select(User).options(selectinload(User.roles)).where(User.email == email)
    )


async def get_user_by_id(session: AsyncSession, user_id: UUID) -> User | None:
    return await session.scalar(
        select(User).options(selectinload(User.roles)).where(User.id == user_id)
    )


async def get_role_by_name(session: AsyncSession, name: str) -> Role | None:
    return await session.scalar(select(Role).where(Role.name == name))
