"""Opt-in, non-transactional demo catalog seed for local development."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import RoleName
from app.exceptions import AuthorizationError, ConflictError
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.models.user import User
from app.repositories.user_repository import get_user_by_id
from app.services.ticket_series_service import PrizeDraft, TicketSeriesService


DEMO_NAME = "DEMO: Catalog Preview (DRAFT)"
DEMO_DESCRIPTION = "Non-production catalog preview. No orders, payments or tickets are seeded."
DEMO_KEY = "demo-draft-series-v1"


@dataclass(slots=True)
class DemoSeedResult:
    series_id: UUID
    existed: bool


class DemoSeedService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def seed_draft_series(self, *, admin_user_id: UUID, commit: bool) -> DemoSeedResult:
        if get_settings().app_env.lower() != "development":
            raise ValueError("Demo data may only be seeded in development")
        admin: User | None = await get_user_by_id(self.session, admin_user_id)
        if admin is None or not admin.is_active or RoleName.ADMIN.value not in {role.name for role in admin.roles}:
            raise AuthorizationError("An active administrator is required")

        existing = await self.session.scalar(select(TicketSeries).where(TicketSeries.name == DEMO_NAME))
        if existing is not None:
            if existing.description != DEMO_DESCRIPTION or existing.status != TicketSeriesStatus.DRAFT:
                raise ConflictError("DEMO_SEED_CONFLICT", "Existing demo name is not an untouched draft")
            return DemoSeedResult(series_id=existing.id, existed=True)

        result = await TicketSeriesService(self.session).create(
            actor_user_id=admin_user_id,
            name=DEMO_NAME,
            description=DEMO_DESCRIPTION,
            price_paise=1_000,
            ticket_limit=100,
            sales_start_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            sales_end_at=datetime(2099, 2, 1, tzinfo=timezone.utc),
            draw_at=datetime(2099, 3, 1, tzinfo=timezone.utc),
            prizes=[PrizeDraft(rank=1, title="Preview prize", prize_paise=1_000)],
            idempotency_key=DEMO_KEY,
            commit=commit,
        )
        return DemoSeedResult(series_id=result.series.id, existed=result.replayed)
