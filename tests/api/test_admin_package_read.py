"""Administrator package reads include inactive records without mutations."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.core.security import create_access_token
from app.database import get_session
from app.main import app
from app.models.ticket_package import TicketPackage
from app.models.user import Role, User


@pytest.mark.asyncio
async def test_admin_package_read_includes_inactive_and_is_role_gated(session: AsyncSession) -> None:
    admin = User(
        email=f"package-admin-{uuid4()}@example.test",
        password_hash="test-only",
        roles=[Role(name=RoleName.ADMIN.value)],
    )
    buyer = User(email=f"package-buyer-{uuid4()}@example.test", password_hash="test-only")
    session.add_all([admin, buyer])
    await session.flush()
    package = TicketPackage(
        name="Inactive package", description="Stored admin catalog entry",
        price_paise=15000, inventory_limit=100, sold_count=0,
        reserved_count=0, currency="INR", is_active=False,
        created_by_user_id=admin.id,
    )
    session.add(package)
    await session.commit()

    async def override_session():
        yield session

    admin_auth = {"Authorization": f"Bearer {create_access_token(str(admin.id), [])}"}
    buyer_auth = {"Authorization": f"Bearer {create_access_token(str(buyer.id), [])}"}
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            denied = await client.get("/api/v1/admin/ticket-packages", headers=buyer_auth)
            listed = await client.get("/api/v1/admin/ticket-packages?limit=1", headers=admin_auth)
            detail = await client.get(f"/api/v1/admin/ticket-packages/{package.id}", headers=admin_auth)
            bad_limit = await client.get("/api/v1/admin/ticket-packages?limit=201", headers=admin_auth)
    finally:
        app.dependency_overrides.clear()

    assert denied.status_code == 403
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
    assert listed.json()[0]["id"] == str(package.id)
    assert listed.json()[0]["is_active"] is False
    assert detail.status_code == 200 and detail.json()["is_active"] is False
    assert bad_limit.status_code == 422
