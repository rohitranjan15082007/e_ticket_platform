"""Shared isolated database fixture for Phase 1 tests."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base
from app.models.ledger import (
    PLATFORM_CLEARING_ACCOUNT_CODE,
    PLATFORM_CLEARING_ACCOUNT_ID,
    PLATFORM_EMERGENCY_RESERVE_ACCOUNT_CODE,
    PLATFORM_EMERGENCY_RESERVE_ACCOUNT_ID,
    PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE,
    PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_ID,
    PLATFORM_MARKETING_ACCOUNT_CODE,
    PLATFORM_MARKETING_ACCOUNT_ID,
    PLATFORM_OPERATIONS_ACCOUNT_CODE,
    PLATFORM_OPERATIONS_ACCOUNT_ID,
    PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE,
    PLATFORM_P2P_ORDER_PENDING_ACCOUNT_ID,
    PLATFORM_PRIZE_POOL_ACCOUNT_CODE,
    PLATFORM_PRIZE_POOL_ACCOUNT_ID,
    PLATFORM_PROFIT_GROWTH_ACCOUNT_CODE,
    PLATFORM_PROFIT_GROWTH_ACCOUNT_ID,
    AccountKind,
    LedgerAccount,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as setup_session:
        setup_session.add(
            LedgerAccount(
                id=PLATFORM_CLEARING_ACCOUNT_ID,
                code=PLATFORM_CLEARING_ACCOUNT_CODE,
                kind=AccountKind.PLATFORM_CLEARING,
                currency="INR",
            )
        )
        setup_session.add(
            LedgerAccount(
                id=PLATFORM_P2P_ORDER_PENDING_ACCOUNT_ID,
                code=PLATFORM_P2P_ORDER_PENDING_ACCOUNT_CODE,
                kind=AccountKind.PLATFORM_P2P_ORDER_PENDING,
                currency="INR",
            )
        )
        setup_session.add(
            LedgerAccount(
                id=PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_ID,
                code=PLATFORM_EXTERNAL_ORDER_PENDING_ACCOUNT_CODE,
                kind=AccountKind.PLATFORM_EXTERNAL_ORDER_PENDING,
                currency="INR",
            )
        )
        setup_session.add_all(
            (
                LedgerAccount(
                    id=PLATFORM_PRIZE_POOL_ACCOUNT_ID,
                    code=PLATFORM_PRIZE_POOL_ACCOUNT_CODE,
                    kind=AccountKind.PLATFORM_PRIZE_POOL,
                    currency="INR",
                ),
                LedgerAccount(
                    id=PLATFORM_MARKETING_ACCOUNT_ID,
                    code=PLATFORM_MARKETING_ACCOUNT_CODE,
                    kind=AccountKind.PLATFORM_MARKETING,
                    currency="INR",
                ),
                LedgerAccount(
                    id=PLATFORM_OPERATIONS_ACCOUNT_ID,
                    code=PLATFORM_OPERATIONS_ACCOUNT_CODE,
                    kind=AccountKind.PLATFORM_OPERATIONS,
                    currency="INR",
                ),
                LedgerAccount(
                    id=PLATFORM_EMERGENCY_RESERVE_ACCOUNT_ID,
                    code=PLATFORM_EMERGENCY_RESERVE_ACCOUNT_CODE,
                    kind=AccountKind.PLATFORM_EMERGENCY_RESERVE,
                    currency="INR",
                ),
                LedgerAccount(
                    id=PLATFORM_PROFIT_GROWTH_ACCOUNT_ID,
                    code=PLATFORM_PROFIT_GROWTH_ACCOUNT_CODE,
                    kind=AccountKind.PLATFORM_PROFIT_GROWTH,
                    currency="INR",
                ),
            )
        )
        await setup_session.commit()
    async with session_factory() as database_session:
        yield database_session
    await engine.dispose()
