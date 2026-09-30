"""Read/lock helpers for ticket catalog and finite inventory rows."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ticket import Ticket
from app.models.ticket_package import TicketPackage, TicketPackageItem
from app.models.ticket_series import TicketSeries, TicketSeriesStatus


_SERIES_LOAD = selectinload(TicketSeries.prizes)
_PACKAGE_LOAD = selectinload(TicketPackage.items).selectinload(TicketPackageItem.series)


async def get_ticket_series(
    session: AsyncSession, series_id: UUID, *, for_update: bool = False
) -> TicketSeries | None:
    statement = select(TicketSeries).options(_SERIES_LOAD).where(TicketSeries.id == series_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def get_ticket_series_locked_many(
    session: AsyncSession, series_ids: Sequence[UUID]
) -> list[TicketSeries]:
    """Lock finite inventory rows in UUID order to avoid allocation deadlocks."""

    ordered_ids = sorted(set(series_ids), key=str)
    if not ordered_ids:
        return []
    rows = (
        await session.scalars(
            select(TicketSeries)
            .options(_SERIES_LOAD)
            .where(TicketSeries.id.in_(ordered_ids))
            .order_by(TicketSeries.id)
            .with_for_update()
        )
    ).all()
    return sorted(rows, key=lambda series: str(series.id))


async def list_open_ticket_series(session: AsyncSession, *, now: datetime) -> list[TicketSeries]:
    return (
        await session.scalars(
            select(TicketSeries)
            .options(_SERIES_LOAD)
            .where(
                TicketSeries.status == TicketSeriesStatus.OPEN,
                TicketSeries.sales_start_at <= now,
                TicketSeries.sales_end_at > now,
            )
            .order_by(TicketSeries.sales_end_at, TicketSeries.id)
        )
    ).all()


async def get_ticket_package(
    session: AsyncSession, package_id: UUID, *, for_update: bool = False
) -> TicketPackage | None:
    statement = select(TicketPackage).options(_PACKAGE_LOAD).where(TicketPackage.id == package_id)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def list_active_ticket_packages(session: AsyncSession) -> list[TicketPackage]:
    return (
        await session.scalars(
            select(TicketPackage)
            .options(_PACKAGE_LOAD)
            .where(TicketPackage.is_active.is_(True))
            .order_by(TicketPackage.name, TicketPackage.id)
        )
    ).all()


async def list_admin_ticket_packages(session: AsyncSession, *, limit: int) -> list[TicketPackage]:
    """Bounded administrator catalog includes inactive records."""
    return (
        await session.scalars(
            select(TicketPackage)
            .options(_PACKAGE_LOAD)
            .order_by(TicketPackage.created_at.desc(), TicketPackage.id.desc())
            .limit(limit)
        )
    ).all()


async def next_ticket_serial_start(session: AsyncSession, series_id: UUID) -> int:
    """Return the next serial after the caller has locked the series row."""

    current = await session.scalar(
        select(func.coalesce(func.max(Ticket.serial_number), 0)).where(Ticket.series_id == series_id)
    )
    return int(current or 0) + 1


async def list_tickets_for_owner(session: AsyncSession, owner_user_id: UUID) -> list[Ticket]:
    return (
        await session.scalars(
            select(Ticket)
            .options(selectinload(Ticket.series), selectinload(Ticket.order_item))
            .where(Ticket.owner_user_id == owner_user_id)
            .order_by(Ticket.created_at.desc(), Ticket.id)
        )
    ).all()
