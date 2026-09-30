"""Owner-scoped in-app event reads omit durable payloads."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.database import get_session
from app.main import app
from app.models.p2p_match import P2PNotification, P2POutboxEvent
from app.models.user import User


@pytest.mark.asyncio
async def test_notifications_are_owner_scoped_bounded_and_payload_free(session: AsyncSession) -> None:
    buyer = User(email=f"notice-buyer-{uuid4()}@example.test", password_hash="test-only")
    other = User(email=f"notice-other-{uuid4()}@example.test", password_hash="test-only")
    event = P2POutboxEvent(
        aggregate_type="match", aggregate_id=uuid4(), event_type="MATCH_REVIEW",
        deduplication_key=f"notice-{uuid4()}", payload={"secret": "outbox-private"},
    )
    session.add_all([buyer, other, event])
    await session.flush()
    session.add_all([
        P2PNotification(
            outbox_event_id=event.id, user_id=buyer.id, event_type="MATCH_REVIEW",
            payload={"secret": "buyer-private"}, delivered_at=datetime.now(timezone.utc),
        ),
        P2PNotification(
            outbox_event_id=event.id, user_id=other.id, event_type="OTHER_USER_EVENT",
            payload={"secret": "other-private"}, delivered_at=datetime.now(timezone.utc),
        ),
    ])
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            anonymous = await client.get("/api/v1/notifications")
            listed = await client.get(
                "/api/v1/notifications?limit=1",
                headers={"Authorization": f"Bearer {create_access_token(str(buyer.id), [])}"},
            )
            bad_limit = await client.get(
                "/api/v1/notifications?limit=101",
                headers={"Authorization": f"Bearer {create_access_token(str(buyer.id), [])}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert anonymous.status_code == 401
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
    assert listed.json()[0]["event_type"] == "MATCH_REVIEW"
    assert set(listed.json()[0]) == {"id", "source", "event_type", "occurred_at"}
    assert "private" not in listed.text
    assert "OTHER_USER_EVENT" not in listed.text
    assert bad_limit.status_code == 422
