"""Transactional P2P outbox dispatcher for delivery and in-app notices."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditService
from app.exceptions import AppError, ConflictError, ValidationError
from app.models.p2p_match import OutboxEventStatus, P2POutboxEvent
from app.repositories.outbox_repository import get_outbox_event, list_dispatchable_outbox_event_ids
from app.services.notification_service import P2PNotificationService
from app.services.p2p_service import P2PService


class P2POutboxService:
    """Dispatch only local/durable actions; it never calls an unconfigured provider."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.p2p = P2PService(session)
        self.notifications = P2PNotificationService(session)

    async def dispatch_pending(self, *, limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1_000:
            raise ValidationError("INVALID_BATCH_SIZE", "limit must be an integer between 1 and 1000")
        event_ids = await list_dispatchable_outbox_event_ids(self.session, limit=limit)
        delivered = 0
        for event_id in event_ids:
            if await self.dispatch_event(event_id=event_id, commit=True):
                delivered += 1
        return delivered

    async def dispatch_event(self, *, event_id: UUID, commit: bool) -> bool:
        event = await get_outbox_event(self.session, event_id, for_update=True)
        if event is None:
            raise ValidationError("UNKNOWN_OUTBOX_EVENT", "P2P outbox event does not exist")
        if event.status == OutboxEventStatus.DELIVERED:
            return False
        if event.status == OutboxEventStatus.PROCESSING:
            # A live transaction owns this event.  It remains retryable if that
            # transaction rolls back, so another worker must not steal it.
            return False

        before = P2PService.outbox_state(event)
        event.status = OutboxEventStatus.PROCESSING
        event.attempts += 1
        try:
            if event.event_type == "P2P_ORDER_DELIVERY_REQUESTED":
                await self._dispatch_delivery(event)
            elif event.event_type == "P2P_SUPPORTED_SETTLEMENT_REQUESTED":
                await self._dispatch_supported_settlement(event)
            elif event.event_type == "P2P_NOTIFICATION_REQUESTED":
                await self._dispatch_notification(event)
            else:
                raise ValidationError("UNKNOWN_OUTBOX_EVENT_TYPE", "Unsupported P2P outbox event type")
        except AppError as error:
            event.status = OutboxEventStatus.FAILED
            event.last_error = f"{error.code}: {error.message}"[:500]
            self.audit.record(
                actor_user_id=None,
                entity_type="p2p_outbox_event",
                entity_id=event.id,
                action="P2P_OUTBOX_DISPATCH_FAILED",
                before_state=before,
                after_state=P2PService.outbox_state(event),
                reason=event.last_error,
            )
            await self._finish(commit=commit)
            return False

        event.status = OutboxEventStatus.DELIVERED
        event.last_error = None
        event.delivered_at = datetime.now(timezone.utc)
        self.audit.record(
            actor_user_id=None,
            entity_type="p2p_outbox_event",
            entity_id=event.id,
            action="P2P_OUTBOX_DISPATCHED",
            before_state=before,
            after_state=P2PService.outbox_state(event),
        )
        await self._finish(commit=commit)
        return True

    async def _dispatch_delivery(self, event: P2POutboxEvent) -> None:
        order_id = self._uuid_payload(event, "order_id")
        response = await self.p2p.deliver_paid_order_from_outbox(
            order_id=order_id,
            outbox_attempt_key=f"p2p-outbox-delivery:{event.id}:{event.attempts}",
            commit=False,
        )
        buyer_value = response.get("buyer_user_id")
        try:
            buyer_user_id = UUID(str(buyer_value))
        except (TypeError, ValueError) as error:
            raise ConflictError("ORDER_INCONSISTENT", "Delivered order has no valid buyer identity") from error
        self.p2p._enqueue_notification(
            aggregate_type="order",
            aggregate_id=order_id,
            notification_type="P2P_DELIVERY_COMPLETED",
            recipient_user_ids=[buyer_user_id],
            payload={"order_id": order_id, "source_outbox_event_id": event.id},
            deduplication_key=f"p2p-outbox:{event.id}:notification:delivery-completed",
            actor_user_id=None,
        )

    async def _dispatch_supported_settlement(self, event: P2POutboxEvent) -> None:
        match_id = self._uuid_payload(event, "match_id")
        payment_submission_id = self._uuid_payload(event, "payment_submission_id")
        source_value = event.payload.get("verification_source")
        verification_source = (
            source_value.strip()
            if isinstance(source_value, str) and source_value.strip()
            else "PROVIDER_OUTBOX"
        )
        await self.p2p.settle_supported_verified_payment(
            match_id=match_id,
            actor_user_id=None,
            payment_submission_id=payment_submission_id,
            verification_source=verification_source,
            idempotency_key=f"p2p-outbox-settlement:{event.id}:{event.attempts}",
            commit=False,
        )

    async def _dispatch_notification(self, event: P2POutboxEvent) -> None:
        await self.notifications.deliver_from_outbox(event=event)

    @staticmethod
    def _uuid_payload(event: P2POutboxEvent, key: str) -> UUID:
        value = event.payload.get(key)
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as error:
            raise ValidationError("INVALID_OUTBOX_PAYLOAD", f"P2P outbox event has invalid {key}") from error

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()
