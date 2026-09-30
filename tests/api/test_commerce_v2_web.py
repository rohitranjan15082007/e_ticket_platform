"""Commerce UI shells and non-authoritative payment-method hints."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import create_access_token
from app.database import get_session
from app.main import app
from app.models.user import User


@pytest.mark.asyncio
async def test_commerce_pages_keep_scoped_styles_and_server_shell() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for route in ("/checkout", "/payments", "/wallet", "/withdrawals"):
            response = await client.get(route)
            assert response.status_code == 200, route
            assert 'class="user-v2 commerce-v2"' in response.text, route
            assert 'href="/assets/css/commerce-v2.css"' in response.text, route
            assert 'id="account-sidebar"' in response.text, route
            assert 'id="account-mobile-nav"' in response.text, route
            assert 'id="page-content"' in response.text, route
            assert "script-src 'self'" in response.headers["content-security-policy"]

        style = await client.get("/assets/css/commerce-v2.css")
        assert style.status_code == 200
        assert "checkout-layout" in style.text


@pytest.mark.asyncio
async def test_method_availability_is_authenticated_and_does_not_invent_upi(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TICKET_MANUAL_UPI_ENABLED", "true")
    get_settings.cache_clear()
    user = User(email=f"commerce-v2-{uuid4()}@example.test", password_hash="test-only-hash")
    session.add(user)
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            unauthenticated = await client.get("/api/v1/payment-methods")
            authenticated = await client.get(
                "/api/v1/payment-methods",
                headers={"Authorization": f"Bearer {create_access_token(str(user.id), [])}"},
            )
        assert unauthenticated.status_code == 401
        assert authenticated.status_code == 200
        assert authenticated.json()["manual_upi"] is False  # No active approved destination.
        assert authenticated.json()["white_label"] is False
        assert authenticated.json()["p2p_match"] is True
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
