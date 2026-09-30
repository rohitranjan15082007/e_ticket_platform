"""Account v2 web shells remain same-origin and do not alter payment behavior."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_account_v2_pages_and_assets() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for route in ("/dashboard", "/tickets", "/orders", f"/orders/{uuid4()}"):
            response = await client.get(route)
            assert response.status_code == 200, route
            assert 'class="user-v2"' in response.text, route
            assert 'href="/assets/css/user-v2.css"' in response.text, route
            assert 'id="account-sidebar"' in response.text, route
            assert 'id="account-mobile-nav"' in response.text, route
            assert 'id="page-content"' in response.text, route
            assert "script-src 'self'" in response.headers["content-security-policy"]

        for route in ("/assets/css/user-v2.css", "/assets/js/account_v2.js"):
            response = await client.get(route)
            assert response.status_code == 200, route
            assert "PLANNED:" not in response.text


@pytest.mark.asyncio
async def test_order_detail_route_requires_uuid() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/orders/not-an-id")
    assert response.status_code == 422
