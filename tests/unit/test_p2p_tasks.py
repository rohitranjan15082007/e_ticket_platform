"""Registration checks for durable P2P background work."""

from app.tasks.celery_app import celery_app


def test_p2p_expiry_and_outbox_tasks_are_registered_with_beat() -> None:
    """A worker import exposes both scheduled P2P task names."""

    celery_app.loader.import_default_modules()
    assert "ticket.expire_p2p_matches" in celery_app.tasks
    assert "ticket.dispatch_p2p_outbox" in celery_app.tasks
    schedule = celery_app.conf.beat_schedule
    assert schedule["expire-p2p-matches"]["task"] == "ticket.expire_p2p_matches"
    assert schedule["dispatch-p2p-outbox"]["task"] == "ticket.dispatch_p2p_outbox"
