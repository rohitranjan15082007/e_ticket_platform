"""Read and lock helpers for the auditable draw lifecycle."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.winner import Draw, DrawEntry, DrawNotification, DrawStatus, PrizeAward, Winner


async def get_draw_by_series(
    session: AsyncSession, series_id: UUID, *, for_update: bool = False
) -> Draw | None:
    statement = select(Draw).where(Draw.series_id == series_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def get_draw(session: AsyncSession, draw_id: UUID, *, for_update: bool = False) -> Draw | None:
    statement = select(Draw).where(Draw.id == draw_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def list_draw_entries(
    session: AsyncSession, draw_id: UUID, *, for_update: bool = False
) -> list[DrawEntry]:
    statement = select(DrawEntry).where(DrawEntry.draw_id == draw_id).order_by(DrawEntry.serial_number, DrawEntry.id)
    if for_update:
        statement = statement.with_for_update()
    return list(await session.scalars(statement))


async def count_draw_entries(session: AsyncSession, draw_id: UUID) -> int:
    """Count frozen candidates without materializing a potentially huge manifest."""

    count = await session.scalar(
        select(func.count()).select_from(DrawEntry).where(DrawEntry.draw_id == draw_id)
    )
    return int(count or 0)


async def list_draw_entry_serial_numbers(
    session: AsyncSession, draw_id: UUID, *, after_serial_number: int, limit: int
) -> list[int]:
    """Read one bounded, serial-ordered public candidate-manifest page."""

    statement = (
        select(DrawEntry.serial_number)
        .where(DrawEntry.draw_id == draw_id, DrawEntry.serial_number > after_serial_number)
        .order_by(DrawEntry.serial_number, DrawEntry.id)
        .limit(limit)
    )
    return [int(serial_number) for serial_number in (await session.scalars(statement))]


async def list_draw_entries_for_tickets(
    session: AsyncSession, draw_id: UUID, ticket_ids: Sequence[UUID]
) -> list[DrawEntry]:
    """Read only winner-linked immutable entries, never the whole manifest."""

    if not ticket_ids:
        return []
    return list(
        await session.scalars(
            select(DrawEntry)
            .where(DrawEntry.draw_id == draw_id, DrawEntry.ticket_id.in_(ticket_ids))
            .order_by(DrawEntry.serial_number, DrawEntry.id)
        )
    )


async def list_winners(
    session: AsyncSession, draw_id: UUID, *, for_update: bool = False
) -> list[Winner]:
    statement = select(Winner).where(Winner.draw_id == draw_id).order_by(Winner.rank, Winner.id)
    if for_update:
        statement = statement.with_for_update()
    return list(await session.scalars(statement))


async def list_prize_awards(
    session: AsyncSession, draw_id: UUID, *, for_update: bool = False
) -> list[PrizeAward]:
    statement = select(PrizeAward).where(PrizeAward.draw_id == draw_id).order_by(PrizeAward.created_at, PrizeAward.id)
    if for_update:
        statement = statement.with_for_update()
    return list(await session.scalars(statement))


async def list_draw_notifications(
    session: AsyncSession, draw_id: UUID, *, for_update: bool = False
) -> list[DrawNotification]:
    statement = select(DrawNotification).where(DrawNotification.draw_id == draw_id).order_by(
        DrawNotification.created_at, DrawNotification.id
    )
    if for_update:
        statement = statement.with_for_update()
    return list(await session.scalars(statement))


async def list_published_draws(session: AsyncSession, *, limit: int = 100) -> Sequence[Draw]:
    return list(
        await session.scalars(
            select(Draw)
            .where(Draw.status == DrawStatus.RESULT_PUBLISHED)
            .order_by(Draw.published_at.desc(), Draw.id.desc())
            .limit(limit)
        )
    )
