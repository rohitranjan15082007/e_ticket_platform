"""Wallet and ledger-account retrieval with optional database row locking."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.wallet import Wallet, WalletHold


async def get_wallet_by_id(
    session: AsyncSession, wallet_id: UUID, *, for_update: bool = False
) -> Wallet | None:
    statement = select(Wallet).options(selectinload(Wallet.accounts)).where(Wallet.id == wallet_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def get_wallet_by_user_id(
    session: AsyncSession, user_id: UUID, *, for_update: bool = False
) -> Wallet | None:
    statement = select(Wallet).options(selectinload(Wallet.accounts)).where(Wallet.user_id == user_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def get_wallet_hold_by_reference(
    session: AsyncSession,
    *,
    wallet_id: UUID,
    reference_type: str,
    reference_id: UUID,
    for_update: bool = False,
) -> WalletHold | None:
    statement = select(WalletHold).where(
        WalletHold.wallet_id == wallet_id,
        WalletHold.reference_type == reference_type,
        WalletHold.reference_id == reference_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)
