"""Celery configuration for safe, durable background transitions."""

from celery import Celery

from app.config import get_settings

settings = get_settings()
celery_app = Celery(
    "e_ticket_platform",
    broker=settings.redis_url,
    backend=settings.redis_url,
    # Workers only perform local durable transitions. Generic-payment expiry
    # routes exposed instructions to review, delivery reads the outbox after
    # settlement, and reconciliation records review work without a provider
    # confirmation or an inferred settlement.
    include=[
        "app.tasks.cleanup_tasks",
        "app.tasks.draw_tasks",
        "app.tasks.payment_tasks",
        "app.tasks.reconciliation_tasks",
    ],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "expire-p2p-matches": {
            "task": "ticket.expire_p2p_matches",
            "schedule": settings.p2p_expiry_poll_seconds,
        },
        "dispatch-p2p-outbox": {
            "task": "ticket.dispatch_p2p_outbox",
            "schedule": settings.p2p_outbox_poll_seconds,
        },
        "expire-payment-attempts": {
            "task": "ticket.expire_payment_attempts",
            "schedule": settings.payment_expiry_poll_seconds,
        },
        "dispatch-payment-outbox": {
            "task": "ticket.dispatch_payment_outbox",
            "schedule": settings.payment_outbox_poll_seconds,
        },
        "reconcile-payment-review-queue": {
            "task": "ticket.reconcile_payment_review_queue",
            "schedule": settings.payment_reconciliation_poll_seconds,
        },
        "close-due-draw-sales": {
            "task": "ticket.close_due_draw_sales",
            "schedule": settings.draw_close_poll_seconds,
        },
    },
)
