"""Durable in-app notification delivery for P2P outbox events."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import canonical_payload
from app.exceptions import ValidationError
from app.models.p2p_match import P2PNotification, P2POutboxEvent


class P2PNotificationService:
    """Materialize a locked notification outbox event as in-app notices.

    The dispatcher owns the outbox row lock. The database unique constraint on
    ``(outbox_event_id, user_id)`` provides a second idempotency boundary if a
    worker is retried after a process interruption.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def deliver_from_outbox(self, *, event: P2POutboxEvent) -> int:
        notification_type = event.payload.get("notification_type")
        recipient_values = event.payload.get("recipient_user_ids")
        if not isinstance(notification_type, str) or not notification_type.strip():
            raise ValidationError("INVALID_NOTIFICATION", "Notification outbox event has no notification type")
        if not isinstance(recipient_values, list) or not recipient_values:
            raise ValidationError("INVALID_NOTIFICATION", "Notification outbox event has no recipients")

        recipients = self._recipient_ids(recipient_values)
        created = 0
        for user_id in recipients:
            existing = await self.session.scalar(
                select(P2PNotification).where(
                    P2PNotification.outbox_event_id == event.id,
                    P2PNotification.user_id == user_id,
                )
            )
            if existing is None:
                self.session.add(
                    P2PNotification(
                        id=uuid4(),
                        outbox_event_id=event.id,
                        user_id=user_id,
                        event_type=notification_type.strip(),
                        payload=canonical_payload(event.payload),
                        delivered_at=datetime.now(timezone.utc),
                    )
                )
                created += 1
        return created

    @staticmethod
    def _recipient_ids(values: list[object]) -> list[UUID]:
        recipient_ids: set[UUID] = set()
        for value in values:
            try:
                recipient_ids.add(UUID(str(value)))
            except (TypeError, ValueError) as error:
                raise ValidationError("INVALID_NOTIFICATION", "Notification recipient is invalid") from error
        return sorted(recipient_ids, key=str)
