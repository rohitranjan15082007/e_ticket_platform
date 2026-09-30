"""Idempotent cleanup entry point for expired, unpaid ticket reservations."""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.database import SessionLocal
from app.exceptions import ConflictError
from app.models.order import Order, OrderStatus
from app.services.order_service import OrderService
from app.tasks.celery_app import celery_app


@celery_app.task(name="ticket.cleanup_expired_orders")
def cleanup_expired_orders(*, batch_size: int = 100) -> int:
    """Release only expired unexposed reservations; never touch paid orders.

    A scheduler may enqueue this task on the platform's chosen cadence. It is
    intentionally not a payment/settlement task and makes no external call.
    """

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 0 < batch_size <= 1_000:
        raise ValueError("batch_size must be an integer between 1 and 1000")
    return asyncio.run(_cleanup_expired_orders(batch_size=batch_size))


async def _cleanup_expired_orders(*, batch_size: int) -> int:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        order_ids = list(
            await session.scalars(
                select(Order.id)
                .where(
                    Order.status.in_([OrderStatus.PENDING_PAYMENT, OrderStatus.WAITING_FOR_MATCH]),
                    Order.expires_at <= now,
                )
                .order_by(Order.expires_at, Order.id)
                .limit(batch_size)
            )
        )
        released = 0
        for order_id in order_ids:
            if await _expire_one(session, order_id):
                released += 1
        return released


async def _expire_one(session, order_id: UUID) -> bool:
    try:
        await OrderService(session).expire_pending(
            order_id=order_id,
            idempotency_key=f"order-expiry:{order_id}",
            commit=True,
        )
        return True
    except ConflictError:
        # A concurrent verified settlement/cancellation won the order lock.
        # Cleanup must never overwrite a newer order transition.
        await session.rollback()
        return False
