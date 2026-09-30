"""Scheduled review-queue snapshots for Phase 5 payment reconciliation.

The task intentionally never contacts an unconfigured provider and never
marks an attempt/order settled. It only persists a bounded operational view
of records already routed to review.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.database import SessionLocal
from app.services.reconciliation_service import PaymentReconciliationService
from app.tasks.celery_app import celery_app


@celery_app.task(name="ticket.reconcile_payment_review_queue")
def reconcile_payment_review_queue(*, batch_size: int | None = None) -> int:
    """Persist one safe reconciliation snapshot per open method/provider scope."""

    settings = get_settings()
    effective_batch_size = settings.payment_outbox_batch_size if batch_size is None else batch_size
    if (
        isinstance(effective_batch_size, bool)
        or not isinstance(effective_batch_size, int)
        or not 0 < effective_batch_size <= 1_000
    ):
        raise ValueError("batch_size must be an integer between 1 and 1000")
    return asyncio.run(
        _reconcile_payment_review_queue(
            batch_size=effective_batch_size,
            interval_seconds=settings.payment_reconciliation_poll_seconds,
        )
    )


async def _reconcile_payment_review_queue(*, batch_size: int, interval_seconds: int) -> int:
    """Run review-only reconciliation over a deterministic scheduled window."""

    window_starts_at, window_ends_at = _scheduled_window(
        now=datetime.now(timezone.utc), interval_seconds=interval_seconds
    )
    async with SessionLocal() as session:
        service = PaymentReconciliationService(session)
        scopes = await service.list_open_scopes(limit=batch_size)
        completed = 0
        for scope in scopes:
            # The window and scope form a deterministic retry key. A Celery
            # retry replays its persisted completed run instead of duplicating
            # an operational report or creating a settlement side effect.
            run_key = (
                "scheduled-payment-review:"
                f"{window_ends_at.strftime('%Y%m%dT%H%M%SZ')}:"
                f"{scope.method.value}:{scope.provider_namespace}"
            )
            await service.reconcile_scope(
                method=scope.method,
                provider_namespace=scope.provider_namespace,
                run_key=run_key,
                window_starts_at=window_starts_at,
                window_ends_at=window_ends_at,
                commit=True,
            )
            completed += 1
        return completed


def _scheduled_window(*, now: datetime, interval_seconds: int) -> tuple[datetime, datetime]:
    """Return an aligned UTC window used solely for run idempotency/reporting."""

    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int) or interval_seconds <= 0:
        raise ValueError("interval_seconds must be a positive integer")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    epoch_seconds = int(now.astimezone(timezone.utc).timestamp())
    end_epoch_seconds = epoch_seconds - (epoch_seconds % interval_seconds)
    window_ends_at = datetime.fromtimestamp(end_epoch_seconds, tz=timezone.utc)
    return window_ends_at - timedelta(seconds=interval_seconds), window_ends_at

