"""Locked access to durable P2P outbox events."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.p2p_match import OutboxEventStatus, P2POutboxEvent


async def get_outbox_event(
    session: AsyncSession, event_id: UUID, *, for_update: bool = False
) -> P2POutboxEvent | None:
    statement = select(P2POutboxEvent).where(P2POutboxEvent.id == event_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def list_dispatchable_outbox_event_ids(session: AsyncSession, *, limit: int) -> list[UUID]:
    """Return work IDs without claiming them; each worker locks before handling."""

    return list(
        await session.scalars(
            select(P2POutboxEvent.id)
            .where(P2POutboxEvent.status.in_([OutboxEventStatus.PENDING, OutboxEventStatus.FAILED]))
            .order_by(P2POutboxEvent.created_at, P2POutboxEvent.id)
            .limit(limit)
        )
    )
