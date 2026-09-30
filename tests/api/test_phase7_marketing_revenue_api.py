"""Phase 7 administrator coupon and revenue API boundaries."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.core.security import create_access_token
from app.database import get_session
from app.main import app
from app.models.order import DeliveryStatus, Order, OrderStatus
from app.models.revenue_allocation import RevenueAllocationSource
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.models.user import Role, User
from app.services.revenue_service import RevenueService


async def _user(session: AsyncSession, *, admin: bool = False) -> User:
    roles = [Role(name=RoleName.ADMIN.value)] if admin else []
    user = User(
        email=f"phase7-api-{uuid4()}@example.test",
        password_hash="test-only-hash",
        roles=roles,
    )
    session.add(user)
    await session.commit()
    return user


def _authorization(user: User, *, admin: bool = False) -> dict[str, str]:
    roles = [RoleName.ADMIN.value] if admin else []
    return {"Authorization": f"Bearer {create_access_token(str(user.id), roles)}"}


async def _open_series(session: AsyncSession, *, admin: User) -> TicketSeries:
    now = datetime.now(timezone.utc)
    series = TicketSeries(
        name="Phase 7 API Series",
        description="Direct fixture catalog row",
        price_paise=2_000,
        ticket_limit=10,
        currency="INR",
        sales_start_at=now - timedelta(minutes=1),
        sales_end_at=now + timedelta(hours=1),
        draw_at=now + timedelta(days=1),
        status=TicketSeriesStatus.OPEN,
        created_by_user_id=admin.id,
    )
    session.add(series)
    await session.commit()
    return series


@pytest.mark.asyncio
async def test_admin_coupon_api_prices_a_buyer_order_server_side(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    buyer = await _user(session)
    series = await _open_series(session, admin=admin)

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            forbidden = await client.post(
                "/api/v1/admin/marketing/coupons",
                headers={**_authorization(buyer), "Idempotency-Key": "phase7-api-coupon-buyer-001"},
                json={},
            )
            created = await client.post(
                "/api/v1/admin/marketing/coupons",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "phase7-api-coupon-admin-001"},
                json={
                    "code": "api20",
                    "description": "API percentage coupon",
                    "discount_type": "PERCENT_BPS",
                    "percentage_bps": 2_000,
                    "max_discount_paise": 350,
                    "minimum_order_paise": 500,
                    "usage_limit": 2,
                    "per_user_limit": 1,
                },
            )
            order = await client.post(
                "/api/v1/orders",
                headers={**_authorization(buyer), "Idempotency-Key": "phase7-api-order-coupon-001"},
                json={
                    "product_type": "SERIES",
                    "product_id": str(series.id),
                    "quantity": 1,
                    "coupon_code": " api20 ",
                },
            )
            listed = await client.get(
                "/api/v1/admin/marketing/coupons", headers=_authorization(admin, admin=True)
            )

        assert forbidden.status_code == 403
        assert created.status_code == 201, created.text
        assert created.json()["code"] == "API20"
        assert order.status_code == 201, order.text
        assert (
            order.json()["subtotal_paise"],
            order.json()["discount_paise"],
            order.json()["total_paise"],
            order.json()["coupon_code_snapshot"],
        ) == (2_000, 350, 1_650, "API20")
        assert listed.status_code == 200
        assert listed.json()[0]["active_redemption_count"] == 1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_revenue_api_exposes_only_posted_immutable_allocations(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    buyer = await _user(session)
    now = datetime.now(timezone.utc)
    settlement_id = uuid4()
    order = Order(
        buyer_user_id=buyer.id,
        status=OrderStatus.PAID,
        delivery_status=DeliveryStatus.PENDING,
        subtotal_paise=9_999,
        discount_paise=0,
        total_paise=9_999,
        currency="INR",
        expires_at=now + timedelta(minutes=15),
        settlement_reference_id=settlement_id,
        settled_at=now,
    )
    session.add(order)
    await session.commit()
    await RevenueService(session).allocate_settled_order(
        order=order,
        settlement_reference_id=settlement_id,
        source=RevenueAllocationSource.EXTERNAL_ORDER_PENDING,
        actor_user_id=None,
        commit=True,
    )

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            forbidden = await client.get("/api/v1/admin/revenue/report", headers=_authorization(buyer))
            report = await client.get(
                "/api/v1/admin/revenue/report", headers=_authorization(admin, admin=True)
            )
            allocations = await client.get(
                "/api/v1/admin/revenue/allocations", headers=_authorization(admin, admin=True)
            )

        assert forbidden.status_code == 403
        assert report.status_code == 200, report.text
        assert report.json()["allocation_count"] == 1
        assert report.json()["allocation_base_paise"] == 9_999
        assert allocations.status_code == 200
        assert allocations.json()[0]["order_id"] == str(order.id)
        assert (
            allocations.json()[0]["prize_pool_paise"]
            + allocations.json()[0]["marketing_paise"]
            + allocations.json()[0]["operations_paise"]
            + allocations.json()[0]["reserve_paise"]
            + allocations.json()[0]["profit_growth_paise"]
        ) == 9_999
    finally:
        app.dependency_overrides.clear()
