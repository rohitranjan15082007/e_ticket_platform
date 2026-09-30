"""Read-only public result views backed by published draw evidence."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ValidationError
from app.repositories.ticket_repository import get_ticket_series
from app.repositories.winner_repository import get_draw_by_series, list_published_draws
from app.services.winner_service import WinnerService


class ResultService:
    """Expose only completed published results, never operational draw state."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.winners = WinnerService(session)

    async def list_published(self, *, limit: int = 100) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValidationError("INVALID_RESULT_LIMIT", "limit must be an integer between 1 and 100")
        draws = await list_published_draws(self.session, limit=limit)
        results: list[dict[str, object]] = []
        for draw in draws:
            series = await get_ticket_series(self.session, draw.series_id)
            if series is None:
                # A RESTRICT foreign key should make this impossible. Preserve
                # a controlled failure rather than silently hiding audit data.
                raise ValidationError("RESULT_SERIES_MISSING", "Published result has no ticket series")
            results.append(await self.winners.public_snapshot(series=series, draw=draw))
        return results

    async def get_published(self, *, series_id: UUID) -> dict[str, object]:
        series = await get_ticket_series(self.session, series_id)
        if series is None:
            raise ValidationError("UNKNOWN_TICKET_SERIES", "Ticket series does not exist")
        draw = await get_draw_by_series(self.session, series.id)
        if draw is None:
            raise ValidationError("RESULT_NOT_PUBLISHED", "The requested result has not been published")
        return await self.winners.public_snapshot(series=series, draw=draw)

    async def get_published_candidates(
        self, *, series_id: UUID, after_serial_number: int = 0, limit: int = 1_000
    ) -> dict[str, object]:
        """Return one bounded page of a published draw's candidate manifest."""

        series = await get_ticket_series(self.session, series_id)
        if series is None:
            raise ValidationError("UNKNOWN_TICKET_SERIES", "Ticket series does not exist")
        draw = await get_draw_by_series(self.session, series.id)
        if draw is None:
            raise ValidationError("RESULT_NOT_PUBLISHED", "The requested result has not been published")
        return await self.winners.public_candidate_page(
            series=series,
            draw=draw,
            after_serial_number=after_serial_number,
            limit=limit,
        )
