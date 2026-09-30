"""Role-gated, bounded administrator user reads do not expose credentials."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.core.security import create_access_token
from app.database import get_session
from app.main import app
from app.models.user import Role, User


def auth(user: User, *, claim_admin: bool = False) -> dict[str, str]:
    roles = [RoleName.ADMIN.value] if claim_admin else []
    return {"Authorization": f"Bearer {create_access_token(str(user.id), roles)}"}


@pytest.mark.asyncio
async def test_admin_user_list_and_detail_are_masked_and_role_gated(session: AsyncSession) -> None:
    admin = User(
        email=f"admin-{uuid4()}@example.test",
        password_hash="admin-secret-hash",
        roles=[Role(name=RoleName.ADMIN.value)],
    )
    buyer = User(
        email=f"buyer-{uuid4()}@example.test",
        full_name="Buyer Name",
        password_hash="buyer-secret-hash",
    )
    session.add_all([admin, buyer])
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            anonymous = await client.get("/api/v1/admin/users")
            buyer_response = await client.get("/api/v1/admin/users", headers=auth(buyer))
            forged = await client.get("/api/v1/admin/users", headers=auth(buyer, claim_admin=True))
            listed = await client.get("/api/v1/admin/users?limit=1", headers=auth(admin))
            detail = await client.get(f"/api/v1/admin/users/{buyer.id}", headers=auth(admin))
            bad_limit = await client.get("/api/v1/admin/users?limit=201", headers=auth(admin))
    finally:
        app.dependency_overrides.clear()

    assert anonymous.status_code == 401
    assert buyer_response.status_code == forged.status_code == 403
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert detail.status_code == 200
    assert detail.json()["id"] == str(buyer.id)
    assert detail.json()["email_hint"].startswith("bu***@")
    assert buyer.email not in detail.text
    assert "secret-hash" not in detail.text + listed.text
    assert set(detail.json()) == {
        "id", "email_hint", "full_name", "is_active", "is_verified", "roles", "created_at",
    }
    assert bad_limit.status_code == 422
