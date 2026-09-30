"""Read/lock helpers for signed P2P provider callbacks."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.p2p_match import P2PProviderEvent


async def get_provider_event(
    session: AsyncSession,
    *,
    provider_namespace: str,
    external_event_id: str,
    for_update: bool = False,
) -> P2PProviderEvent | None:
    statement = select(P2PProviderEvent).where(
        P2PProviderEvent.provider_namespace == provider_namespace,
        P2PProviderEvent.external_event_id == external_event_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def get_provider_event_by_id(
    session: AsyncSession, event_id: UUID, *, for_update: bool = False
) -> P2PProviderEvent | None:
    statement = select(P2PProviderEvent).where(P2PProviderEvent.id == event_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)
