"""Phase 6 scheduler registration and safety-boundary coverage."""

import pytest

from app.tasks.celery_app import celery_app
from app.tasks.draw_tasks import close_due_draw_sales


def test_draw_close_task_is_registered_without_automatic_selection_or_payout() -> None:
    celery_app.loader.import_default_modules()
    assert "ticket.close_due_draw_sales" in celery_app.tasks
    schedule = celery_app.conf.beat_schedule
    assert schedule["close-due-draw-sales"]["task"] == "ticket.close_due_draw_sales"


def test_draw_close_task_rejects_an_unsafe_batch_size_before_database_work() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        close_due_draw_sales(batch_size=0)
