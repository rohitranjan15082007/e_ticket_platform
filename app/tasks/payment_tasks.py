"""Durable P2P and Phase 5 payment timeout/outbox handling.

Neither worker infers external payment success or releases an order after
payment instructions have been exposed.
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.exceptions import ConflictError
from app.models.p2p_match import P2PMatch, P2PMatchStatus
from app.repositories.generic_payment_repository import list_payment_attempt_ids_due_for_expiry
from app.services.payment_orchestrator import PaymentOrchestrator
from app.services.payment_outbox_service import PaymentOutboxService
from app.services.p2p_service import P2PService
from app.services.p2p_outbox_service import P2POutboxService
from app.tasks.celery_app import celery_app


@celery_app.task(name="ticket.expire_p2p_matches")
def expire_p2p_matches(*, batch_size: int = 100) -> int:
    """Move overdue P2P attempts to reconciliation/review, never automatic release."""

    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 0 < batch_size <= 1_000:
        raise ValueError("batch_size must be an integer between 1 and 1000")
    return asyncio.run(_expire_p2p_matches(batch_size=batch_size))


async def _expire_p2p_matches(*, batch_size: int) -> int:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        ids = list(
            await session.scalars(
                select(P2PMatch.id)
                .where(
                    (
                        (P2PMatch.status == P2PMatchStatus.WAITING_FOR_PAYMENT)
                        & (P2PMatch.payment_deadline_at <= now)
                    )
                    |
                    (
                        (P2PMatch.status == P2PMatchStatus.WAITING_FOR_RECEIVER_CONFIRMATION)
                        & (P2PMatch.receiver_confirmation_deadline_at.is_not(None))
                        & (P2PMatch.receiver_confirmation_deadline_at <= now)
                    )
                )
                .order_by(P2PMatch.payment_deadline_at, P2PMatch.id)
                .limit(batch_size)
            )
        )
        changed = 0
        for match_id in ids:
            try:
                await P2PService(session).expire_match(
                    match_id=match_id,
                    idempotency_key=f"p2p-expiry:{match_id}",
                    commit=True,
                )
                changed += 1
            except ConflictError:
                await session.rollback()
        return changed


@celery_app.task(name="ticket.dispatch_p2p_outbox")
def dispatch_p2p_outbox(*, batch_size: int | None = None) -> int:
    """Dispatch local P2P delivery/notification work from the durable outbox."""

    from app.config import get_settings

    effective_batch_size = get_settings().p2p_outbox_batch_size if batch_size is None else batch_size
    if (
        isinstance(effective_batch_size, bool)
        or not isinstance(effective_batch_size, int)
        or not 0 < effective_batch_size <= 1_000
    ):
        raise ValueError("batch_size must be an integer between 1 and 1000")
    return asyncio.run(_dispatch_p2p_outbox(batch_size=effective_batch_size))


async def _dispatch_p2p_outbox(*, batch_size: int) -> int:
    async with SessionLocal() as session:
        return await P2POutboxService(session).dispatch_pending(limit=batch_size)


@celery_app.task(name="ticket.expire_payment_attempts")
def expire_payment_attempts(*, batch_size: int | None = None) -> int:
    """Route expired external-payment instructions into review, never release.

    Generic payment instructions may have been acted on outside the platform,
    so a scheduler is only allowed to expire their local acceptance window and
    retain the order for reconciliation. It cannot cancel the order, release
    its reservation, or infer that a provider payment succeeded.
    """

    from app.config import get_settings

    effective_batch_size = (
        get_settings().payment_outbox_batch_size if batch_size is None else batch_size
    )
    _validate_batch_size(effective_batch_size)
    return asyncio.run(_expire_payment_attempts(batch_size=effective_batch_size))


async def _expire_payment_attempts(*, batch_size: int) -> int:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        ids = await list_payment_attempt_ids_due_for_expiry(
            session, before=now, limit=batch_size
        )
        changed = 0
        for attempt_id in ids:
            try:
                if await PaymentOrchestrator(session).expire_payment_attempt(
                    attempt_id=attempt_id,
                    idempotency_key=f"payment-expiry:{attempt_id}",
                    commit=True,
                ):
                    changed += 1
            except ConflictError:
                # A provider/admin transition won the lock. The next task run
                # observes the durable terminal/review state; never overwrite it.
                await session.rollback()
        return changed


@celery_app.task(name="ticket.dispatch_payment_outbox")
def dispatch_payment_outbox(*, batch_size: int | None = None) -> int:
    """Dispatch only post-settlement local delivery work from the durable outbox."""

    from app.config import get_settings

    effective_batch_size = (
        get_settings().payment_outbox_batch_size if batch_size is None else batch_size
    )
    _validate_batch_size(effective_batch_size)
    return asyncio.run(_dispatch_payment_outbox(batch_size=effective_batch_size))


async def _dispatch_payment_outbox(*, batch_size: int) -> int:
    async with SessionLocal() as session:
        return await PaymentOutboxService(session).dispatch_pending(limit=batch_size)


def _validate_batch_size(batch_size: object) -> int:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 0 < batch_size <= 1_000:
        raise ValueError("batch_size must be an integer between 1 and 1000")
    return batch_size
