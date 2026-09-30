"""Read-only catalog availability decisions for public browsing."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ValidationError
from app.models.ticket_package import TicketPackage
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.models.winner import DrawStatus
from app.repositories.ticket_repository import (
    get_ticket_package,
    get_ticket_series,
    list_active_ticket_packages,
    list_open_ticket_series,
)
from app.repositories.winner_repository import get_draw_by_series
from app.services.ticket_series_service import TicketSeriesService


class CatalogService:
    """Expose only products that appear sellable; checkout still locks/rechecks stock."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_available_series(self) -> list[TicketSeries]:
        now = datetime.now(timezone.utc)
        series = await list_open_ticket_series(self.session, now=now)
        available: list[TicketSeries] = []
        for value in series:
            if self._series_is_sellable(value, now=now) and await self._has_public_commitment(value):
                available.append(value)
        return available

    async def get_available_series(self, series_id: UUID) -> TicketSeries:
        series = await get_ticket_series(self.session, series_id)
        if (
            series is None
            or not self._series_is_sellable(series, now=datetime.now(timezone.utc))
            or not await self._has_public_commitment(series)
        ):
            raise ValidationError("UNKNOWN_TICKET_SERIES", "Ticket series is not available")
        return series

    async def public_series_snapshot(self, series: TicketSeries) -> dict[str, object]:
        """Attach the pre-sale commitment to a catalog entry without its seed."""

        draw = await get_draw_by_series(self.session, series.id)
        if draw is None or draw.status != DrawStatus.COMMITTED:
            raise ValidationError("DRAW_COMMITMENT_REQUIRED", "Sellable series must have a public draw commitment")
        snapshot = TicketSeriesService.snapshot(series)
        snapshot["draw_seed_commitment"] = draw.seed_commitment
        snapshot["draw_committed_at"] = draw.committed_at
        return snapshot

    async def list_available_packages(self) -> list[TicketPackage]:
        now = datetime.now(timezone.utc)
        packages = await list_active_ticket_packages(self.session)
        return [package for package in packages if self._package_is_sellable(package, now=now)]

    async def get_available_package(self, package_id: UUID) -> TicketPackage:
        package = await get_ticket_package(self.session, package_id)
        if package is None or not self._package_is_sellable(package, now=datetime.now(timezone.utc)):
            raise ValidationError("UNKNOWN_TICKET_PACKAGE", "Ticket package is not available")
        return package

    @classmethod
    def _package_is_sellable(cls, package: TicketPackage, *, now: datetime) -> bool:
        if not package.is_active or not package.items:
            return False
        if package.inventory_limit is not None and package.sold_count + package.reserved_count >= package.inventory_limit:
            return False
        return all(
            cls._series_is_sellable(item.series, now=now)
            and item.series.ticket_limit - item.series.sold_count - item.series.reserved_count >= item.quantity
            for item in package.items
        )

    @classmethod
    def _series_is_sellable(cls, series: TicketSeries, *, now: datetime) -> bool:
        return (
            series.status == TicketSeriesStatus.OPEN
            and cls._utc(series.sales_start_at) <= now < cls._utc(series.sales_end_at)
            and series.ticket_limit - series.sold_count - series.reserved_count > 0
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    async def _has_public_commitment(self, series: TicketSeries) -> bool:
        draw = await get_draw_by_series(self.session, series.id)
        return draw is not None and draw.status == DrawStatus.COMMITTED
