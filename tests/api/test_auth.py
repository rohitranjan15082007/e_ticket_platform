"""End-to-end API tests for Phase 1 authentication routes."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.main import app


@pytest.mark.asyncio
async def test_register_then_login_through_api(session: AsyncSession) -> None:
    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            registration = await client.post(
                "/api/v1/auth/register",
                headers={"Idempotency-Key": "api-register-key-001"},
                json={
                    "email": "api-user@example.com",
                    "password": "correct-horse-battery-staple",
                    "full_name": "API User",
                },
            )
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": "api-user@example.com", "password": "correct-horse-battery-staple"},
            )

        assert registration.status_code == 201
        assert registration.json()["roles"] == ["user"]
        assert login.status_code == 200
        assert login.json()["token"]["token_type"] == "bearer"
    finally:
        app.dependency_overrides.clear()
