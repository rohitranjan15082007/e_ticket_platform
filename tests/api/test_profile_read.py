"""Authenticated profile returns only the caller's safe identity fields."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.database import get_session
from app.main import app
from app.models.user import User


@pytest.mark.asyncio
async def test_profile_is_owner_scoped_and_excludes_credentials(session: AsyncSession) -> None:
    user = User(
        email=f"profile-{uuid4()}@example.test", full_name="Profile Owner",
        password_hash="private-password-hash", is_verified=True,
    )
    session.add(user)
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            anonymous = await client.get("/api/v1/profile")
            response = await client.get(
                "/api/v1/profile",
                headers={"Authorization": f"Bearer {create_access_token(str(user.id), [])}"},
            )
    finally:
        app.dependency_overrides.clear()

    assert anonymous.status_code == 401
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(user.id)
    assert response.json()["is_verified"] is True
    assert set(response.json()) == {
        "id", "email", "full_name", "is_active", "is_verified", "roles", "created_at",
    }
    assert "private-password-hash" not in response.text
