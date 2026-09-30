"""Registration and deterministic-window checks for Phase 5 background work."""

from datetime import datetime, timezone

from app.tasks.celery_app import celery_app
from app.tasks.reconciliation_tasks import _scheduled_window


def test_phase5_payment_tasks_are_registered_with_safe_beat_schedule() -> None:
    """Workers expose expiry, delivery, and review-only reconciliation tasks."""

    celery_app.loader.import_default_modules()
    assert "ticket.expire_payment_attempts" in celery_app.tasks
    assert "ticket.dispatch_payment_outbox" in celery_app.tasks
    assert "ticket.reconcile_payment_review_queue" in celery_app.tasks
    schedule = celery_app.conf.beat_schedule
    assert schedule["expire-payment-attempts"]["task"] == "ticket.expire_payment_attempts"
    assert schedule["dispatch-payment-outbox"]["task"] == "ticket.dispatch_payment_outbox"
    assert schedule["reconcile-payment-review-queue"]["task"] == "ticket.reconcile_payment_review_queue"


def test_reconciliation_window_is_utc_aligned_and_repeatable() -> None:
    """The same scheduler interval produces the same idempotency window key."""

    start, end = _scheduled_window(
        now=datetime(2026, 9, 23, 12, 34, 56, tzinfo=timezone.utc),
        interval_seconds=300,
    )
    assert start == datetime(2026, 9, 23, 12, 25, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 23, 12, 30, tzinfo=timezone.utc)
