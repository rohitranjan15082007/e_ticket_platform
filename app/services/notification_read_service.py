"""Owner-scoped read of existing durable P2P and draw notifications."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import InAppNotification
from app.models.p2p_match import P2PNotification
from app.models.winner import DrawNotification


class NotificationReadService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_user(self, *, user_id: UUID, limit: int) -> list[InAppNotification]:
        p2p = (await self.session.scalars(
            select(P2PNotification)
            .where(P2PNotification.user_id == user_id)
            .order_by(P2PNotification.delivered_at.desc(), P2PNotification.id.desc())
            .limit(limit)
        )).all()
        draws = (await self.session.scalars(
            select(DrawNotification)
            .where(DrawNotification.user_id == user_id, DrawNotification.delivered_at.is_not(None))
            .order_by(DrawNotification.delivered_at.desc(), DrawNotification.id.desc())
            .limit(limit)
        )).all()
        rows = [InAppNotification.from_p2p(row) for row in p2p]
        rows.extend(InAppNotification.from_draw(row) for row in draws)
        rows.sort(key=lambda row: (row.occurred_at, str(row.id)), reverse=True)
        return rows[:limit]
