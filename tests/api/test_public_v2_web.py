"""The first v2 UI batch keeps its same-origin routes and shared shell."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_public_v2_pages_load_scoped_styles_and_accessible_shell() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for route in ("/", "/series", "/packages", "/results", "/login", "/register"):
            response = await client.get(route)
            assert response.status_code == 200, route
            assert 'class="public-v2"' in response.text, route
            assert 'href="/assets/css/public-v2.css"' in response.text, route
            assert 'href="#main"' in response.text, route
            assert 'id="status"' in response.text, route
            assert 'id="page-content"' in response.text, route
            assert 'src="/assets/js/app.js"' in response.text, route

        styles = await client.get("/assets/css/public-v2.css")
        assert styles.status_code == 200
        assert "body.public-v2" in styles.text
        assert "@media (max-width: 720px)" in styles.text


@pytest.mark.asyncio
async def test_auth_and_result_page_landmarks() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.get("/login")
        register = await client.get("/register")
        results = await client.get("/results")
        demo = await client.get("/payment-demo")

    for page in (login, register):
        assert 'class="auth-layout"' in page.text
        assert 'class="auth-panel"' in page.text
        assert 'aria-labelledby="auth-title"' in page.text
    assert 'aria-current="page"' in results.text
    assert "public-v2.css" not in demo.text
