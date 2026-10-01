"""Async SQLAlchemy database configuration."""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.database_connection import async_database_options

settings = get_settings()
engine_url, connect_args = async_database_options(settings.database_url)
engine = create_async_engine(engine_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Provide one unit-of-work session per request."""

    async with SessionLocal() as session:
        yield session


async def verify_database_connection() -> None:
    """Fail fast when an explicit readiness check cannot reach the database."""

    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def close_database() -> None:
    """Dispose connections during controlled application shutdown."""

    await engine.dispose()
