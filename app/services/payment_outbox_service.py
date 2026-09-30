"""Durable generic-payment delivery dispatcher.

This is intentionally separate from the Phase 4 P2P outbox: P2P settlement
has a withdrawal/receiver confirmation lifecycle, while a provider payment is
an external receipt.  Both may reuse the same entitlement allocator only
after their own durable settlement bridge has marked an order paid.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import AuditService
from app.exceptions import AppError, ConflictError, ValidationError
from app.models.order import DeliveryStatus, OrderStatus
from app.models.payment import PaymentOutboxEvent, PaymentOutboxEventStatus
from app.repositories.generic_payment_repository import (
    get_payment_outbox_event,
    get_payment_settlement_for_order,
    list_dispatchable_payment_outbox_ids,
)
from app.repositories.order_repository import get_order
from app.services.ticket_allocation_service import TicketAllocationService


PAYMENT_ORDER_DELIVERY_REQUESTED = "PAYMENT_ORDER_DELIVERY_REQUESTED"


class PaymentOutboxService:
    """Retry post-settlement local work without re-verifying or re-settling money."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    async def dispatch_pending(self, *, limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1_000:
            raise ValidationError("INVALID_BATCH_SIZE", "limit must be an integer between 1 and 1000")
        ids = await list_dispatchable_payment_outbox_ids(
            self.session, before=datetime.now(timezone.utc), limit=limit
        )
        delivered = 0
        for event_id in ids:
            if await self.dispatch_event(event_id=event_id, commit=True):
                delivered += 1
        return delivered

    async def dispatch_event(self, *, event_id: UUID, commit: bool) -> bool:
        event = await get_payment_outbox_event(self.session, event_id=event_id, for_update=True)
        if event is None:
            raise ValidationError("UNKNOWN_OUTBOX_EVENT", "Payment outbox event does not exist")
        if event.status == PaymentOutboxEventStatus.DELIVERED:
            return False
        if event.status == PaymentOutboxEventStatus.PROCESSING:
            # A still-open transaction owns it.  If that transaction rolls
            # back, the state rolls back too, so do not steal the work.
            return False

        before = self.outbox_state(event)
        event.status = PaymentOutboxEventStatus.PROCESSING
        event.attempts += 1
        try:
            if event.event_type != PAYMENT_ORDER_DELIVERY_REQUESTED:
                raise ValidationError("UNKNOWN_OUTBOX_EVENT_TYPE", "Unsupported payment outbox event type")
            await self._dispatch_delivery(event)
        except AppError as error:
            await self._mark_failed(event=event, before=before, error=f"{error.code}: {error.message}", commit=commit)
            return False
        except Exception as error:  # preserve the durable paid state even for a local delivery defect
            await self._mark_failed(
                event=event,
                before=before,
                error=f"UNEXPECTED_DELIVERY_FAILURE: {type(error).__name__}: {error}",
                commit=commit,
            )
            return False

        event.status = PaymentOutboxEventStatus.DELIVERED
        event.last_error = None
        event.available_at = None
        event.delivered_at = datetime.now(timezone.utc)
        self.audit.record(
            actor_user_id=None,
            entity_type="payment_outbox_event",
            entity_id=event.id,
            action="PAYMENT_OUTBOX_DISPATCHED",
            before_state=before,
            after_state=self.outbox_state(event),
        )
        await self._finish(commit=commit)
        return True

    async def _dispatch_delivery(self, event: PaymentOutboxEvent) -> None:
        order_id = self._uuid_payload(event, "order_id")
        settlement_id = self._uuid_payload(event, "settlement_id")
        settlement = await get_payment_settlement_for_order(self.session, order_id=order_id, for_update=True)
        if settlement is None or settlement.id != settlement_id:
            raise ConflictError("UNVERIFIED_SETTLEMENT", "Payment outbox event does not name the order settlement")
        order = await get_order(self.session, order_id, for_update=True)
        if order is None:
            raise ValidationError("UNKNOWN_ORDER", "Payment outbox event names an unknown order")
        if order.status == OrderStatus.FULFILLED and order.delivery_status == DeliveryStatus.DELIVERED:
            return
        if (
            order.status != OrderStatus.PAID
            or order.settlement_reference_id != settlement.id
            or order.settled_at is None
        ):
            raise ConflictError("ORDER_NOT_SETTLED", "Only a matching paid order can be delivered")

        try:
            # Allocation uses its own locks/idempotency record.  A savepoint
            # keeps this outer transaction usable to mark the outbox retryable
            # if stock/allocation has a later operational failure.
            async with self.session.begin_nested():
                await TicketAllocationService(self.session).allocate_paid_order(
                    order_id=order.id,
                    settlement_reference_id=settlement.id,
                    idempotency_key=f"payment-outbox-delivery:{event.id}:{event.attempts}",
                    actor_user_id=None,
                    commit=False,
                )
        except Exception:
            failed_order = await get_order(self.session, order.id, for_update=True)
            if failed_order is None or failed_order.status != OrderStatus.PAID:
                raise
            before_order = self.order_state(failed_order)
            failed_order.delivery_status = DeliveryStatus.FAILED
            self.audit.record(
                actor_user_id=None,
                entity_type="order",
                entity_id=failed_order.id,
                action="PAYMENT_ORDER_DELIVERY_FAILED",
                before_state=before_order,
                after_state=self.order_state(failed_order),
                reason="Ticket allocation failed after a durable payment settlement",
            )
            raise

    async def _mark_failed(
        self,
        *,
        event: PaymentOutboxEvent,
        before: dict[str, object],
        error: str,
        commit: bool,
    ) -> None:
        event.status = PaymentOutboxEventStatus.FAILED
        event.last_error = error[:500]
        # Bounded exponential backoff; this does not change a payment/order.
        event.available_at = datetime.now(timezone.utc) + timedelta(seconds=min(2 ** event.attempts, 3600))
        self.audit.record(
            actor_user_id=None,
            entity_type="payment_outbox_event",
            entity_id=event.id,
            action="PAYMENT_OUTBOX_DISPATCH_FAILED",
            before_state=before,
            after_state=self.outbox_state(event),
            reason=event.last_error,
        )
        await self._finish(commit=commit)

    @staticmethod
    def _uuid_payload(event: PaymentOutboxEvent, key: str) -> UUID:
        try:
            return UUID(str(event.payload.get(key)))
        except (TypeError, ValueError) as error:
            raise ValidationError("INVALID_OUTBOX_PAYLOAD", f"Payment outbox event has invalid {key}") from error

    async def _finish(self, *, commit: bool) -> None:
        await self.session.flush()
        if commit:
            await self.session.commit()

    @staticmethod
    def outbox_state(event: PaymentOutboxEvent) -> dict[str, object]:
        return {
            "id": event.id,
            "payment_attempt_id": event.payment_attempt_id,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "event_type": event.event_type,
            "status": event.status,
            "attempts": event.attempts,
            "available_at": event.available_at,
            "last_error": event.last_error,
            "delivered_at": event.delivered_at,
        }

    @staticmethod
    def order_state(order) -> dict[str, object]:
        return {
            "id": order.id,
            "status": order.status,
            "delivery_status": order.delivery_status,
            "settlement_reference_id": order.settlement_reference_id,
            "settled_at": order.settled_at,
        }
