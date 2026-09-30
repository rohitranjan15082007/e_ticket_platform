"""Safe, time-based sales closure for pre-committed draw workflows."""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.exceptions import ConflictError
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.services.winner_service import WinnerService
from app.tasks.celery_app import celery_app


@celery_app.task(name="ticket.close_due_draw_sales")
def close_due_draw_sales(*, batch_size: int | None = None) -> int:
    """Freeze only due, committed series; never advance a draw beyond closure."""

    effective_batch_size = get_settings().draw_close_batch_size if batch_size is None else batch_size
    if (
        isinstance(effective_batch_size, bool)
        or not isinstance(effective_batch_size, int)
        or not 0 < effective_batch_size <= 1_000
    ):
        raise ValueError("batch_size must be an integer between 1 and 1000")
    return asyncio.run(_close_due_draw_sales(batch_size=effective_batch_size))


async def _close_due_draw_sales(*, batch_size: int) -> int:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        series_ids = list(
            await session.scalars(
                select(TicketSeries.id)
                .where(
                    TicketSeries.status == TicketSeriesStatus.OPEN,
                    TicketSeries.sales_end_at <= now,
                )
                .order_by(TicketSeries.sales_end_at, TicketSeries.id)
                .limit(batch_size)
            )
        )
        changed = 0
        for series_id in series_ids:
            try:
                await WinnerService(session).close_sales(
                    series_id=series_id,
                    actor_user_id=None,
                    idempotency_key=f"draw-close:{series_id}",
                    commit=True,
                )
                changed += 1
            except ConflictError:
                # A commitment, reservation, or competing transition prevents
                # closure. The next scheduled run re-reads authoritative rows.
                await session.rollback()
        return changed
