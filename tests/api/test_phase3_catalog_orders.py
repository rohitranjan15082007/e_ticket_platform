"""API coverage for safe Phase 3 catalog/order boundaries."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.core.security import create_access_token
from app.database import get_session
from app.main import app
from app.models.user import Role, User


async def _user(session: AsyncSession, *, admin: bool) -> User:
    roles = [Role(name=RoleName.ADMIN.value)] if admin else []
    user = User(
        email=f"phase3-{uuid4()}@example.test",
        password_hash="test-only-hash",
        roles=roles,
    )
    session.add(user)
    await session.commit()
    return user


def _authorization(user: User, *, admin: bool) -> dict[str, str]:
    roles = [RoleName.ADMIN.value] if admin else []
    return {"Authorization": f"Bearer {create_access_token(str(user.id), roles)}"}


@pytest.mark.asyncio
async def test_admin_catalog_then_buyer_order_api_has_no_settlement_route(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    buyer = await _user(session, admin=False)

    async def override_session():
        yield session

    now = datetime.now(timezone.utc)
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            forbidden_admin = await client.post(
                "/api/v1/admin/ticket-series",
                headers={**_authorization(buyer, admin=False), "Idempotency-Key": "buyer-not-admin-0001"},
                json={},
            )
            create = await client.post(
                "/api/v1/admin/ticket-series",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "api-series-create-001"},
                json={
                    "name": "API Series",
                    "description": "Created through the safe catalog API",
                    "price_paise": 3_000,
                    "ticket_limit": 4,
                    "sales_start_at": (now - timedelta(minutes=1)).isoformat(),
                    "sales_end_at": (now + timedelta(hours=1)).isoformat(),
                    "draw_at": (now + timedelta(days=1)).isoformat(),
                    "prizes": [{"rank": 1, "title": "Top", "prize_paise": 5_000}],
                },
            )
            series_id = create.json()["id"]
            publish = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/publish",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "api-series-publish-01"},
            )
            create_package = await client.post(
                "/api/v1/admin/ticket-packages",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "api-package-create-01"},
                json={
                    "name": "API Package",
                    "description": "Created through the guarded admin package API",
                    "price_paise": 5_000,
                    "inventory_limit": 2,
                    "items": [{"series_id": series_id, "quantity": 1}],
                },
            )
            draw_commitment = sha256(b"phase3-api-public-draw-seed").hexdigest()
            commit_draw = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/draw/commit",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "api-series-draw-commit-01"},
                json={"seed_commitment": draw_commitment},
            )
            open_series = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/open",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "api-series-open-0001"},
            )
            catalog = await client.get("/api/v1/catalog/series")
            client_price = await client.post(
                "/api/v1/orders",
                headers={**_authorization(buyer, admin=False), "Idempotency-Key": "api-order-price-0001"},
                json={
                    "product_type": "SERIES", "product_id": series_id, "quantity": 2, "price_paise": 1,
                },
            )
            order = await client.post(
                "/api/v1/orders",
                headers={**_authorization(buyer, admin=False), "Idempotency-Key": "api-order-create-0001"},
                json={"product_type": "SERIES", "product_id": series_id, "quantity": 2},
            )
            fake_settlement = await client.post(
                f"/api/v1/orders/{order.json()['id']}/settle",
                headers={**_authorization(buyer, admin=False), "Idempotency-Key": "not-a-real-settlement"},
            )

        assert forbidden_admin.status_code == 403
        assert create.status_code == 201, create.text
        assert publish.status_code == 200, publish.text
        assert create_package.status_code == 201, create_package.text
        assert create_package.json()["items"] == [{"series_id": series_id, "quantity": 1}]
        assert commit_draw.status_code == 201, commit_draw.text
        assert open_series.status_code == 200, open_series.text
        assert catalog.status_code == 200 and [item["id"] for item in catalog.json()] == [series_id]
        assert catalog.json()[0]["draw_seed_commitment"] == draw_commitment
        assert catalog.json()[0]["draw_committed_at"] is not None
        assert client_price.status_code == 422
        assert order.status_code == 201, order.text
        assert order.json()["status"] == "PENDING_PAYMENT"
        assert fake_settlement.status_code == 404
    finally:
        app.dependency_overrides.clear()
