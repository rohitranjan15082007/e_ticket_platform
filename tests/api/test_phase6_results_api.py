"""API coverage for authorized draw control and public published results."""

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
from app.models.order import DeliveryStatus, Order, OrderItem, OrderStatus, TicketProductType
from app.models.ticket import Ticket, TicketStatus
from app.models.ticket_series import TicketSeries, TicketSeriesPrize, TicketSeriesStatus
from app.models.user import Role, User


def _authorization(user: User, *, admin: bool) -> dict[str, str]:
    roles = [RoleName.ADMIN.value] if admin else []
    return {"Authorization": f"Bearer {create_access_token(str(user.id), roles)}"}


async def _api_draw_fixture(session: AsyncSession) -> tuple[User, User, TicketSeries, str]:
    now = datetime.now(timezone.utc)
    role = Role(id=uuid4(), name=RoleName.ADMIN.value)
    admin = User(id=uuid4(), email=f"phase6-admin-{uuid4()}@example.test", password_hash="test", roles=[role])
    buyer = User(id=uuid4(), email=f"phase6-buyer-{uuid4()}@example.test", password_hash="test")
    series = TicketSeries(
        id=uuid4(),
        name="Phase 6 API series",
        description=None,
        price_paise=3_000,
        ticket_limit=5,
        sold_count=0,
        reserved_count=0,
        currency="INR",
        sales_start_at=now - timedelta(hours=1),
        sales_end_at=now + timedelta(minutes=5),
        draw_at=now + timedelta(minutes=10),
        status=TicketSeriesStatus.PUBLISHED,
        created_by_user_id=admin.id,
    )
    prize = TicketSeriesPrize(id=uuid4(), series_id=series.id, rank=1, title="Winner", prize_paise=10_000)
    session.add_all([role, admin, buyer, series, prize])
    await session.commit()
    return admin, buyer, series, "phase6-api-seed-value-0001"


async def _record_api_tickets(session: AsyncSession, *, series: TicketSeries, buyer: User) -> None:
    now = datetime.now(timezone.utc)
    order = Order(
        id=uuid4(), buyer_user_id=buyer.id, status=OrderStatus.FULFILLED,
        delivery_status=DeliveryStatus.DELIVERED, total_paise=6_000, currency="INR",
        expires_at=now + timedelta(minutes=5), settlement_reference_id=uuid4(), settled_at=now,
    )
    item = OrderItem(
        id=uuid4(), order_id=order.id, product_type=TicketProductType.SERIES,
        product_id=series.id, product_name_snapshot=series.name, unit_price_paise=3_000,
        quantity=2, line_total_paise=6_000, currency="INR",
    )
    session.add_all(
        [
            order,
            item,
            *[
                Ticket(
                    id=uuid4(), series_id=series.id, serial_number=serial, order_item_id=item.id,
                    owner_user_id=buyer.id, status=TicketStatus.ALLOCATED, is_winner=False, prize_paise=0,
                )
                for serial in (1, 2)
            ],
        ]
    )
    series.sold_count = 2
    await session.commit()


@pytest.mark.asyncio
async def test_phase6_admin_draw_api_and_public_result_endpoint(session: AsyncSession) -> None:
    admin, buyer, series, seed = await _api_draw_fixture(session)
    series_id = series.id

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            forbidden = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/draw/commit",
                headers={**_authorization(buyer, admin=False), "Idempotency-Key": "phase6-api-denied-1"},
                json={"seed_commitment": sha256(seed.encode("utf-8")).hexdigest()},
            )
            commit = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/draw/commit",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "phase6-api-commit-1"},
                json={"seed_commitment": sha256(seed.encode("utf-8")).hexdigest()},
            )
            assert forbidden.status_code == 403
            assert commit.status_code == 201, commit.text
            assert commit.json()["seed_revealed"] is False
            opening = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/open",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "phase6-api-open-1"},
            )
            assert opening.status_code == 200, opening.text
            assert opening.json()["status"] == "OPEN"
            admin_view = await client.get(
                f"/api/v1/admin/ticket-series/{series_id}/draw",
                headers=_authorization(admin, admin=True),
            )
            assert admin_view.status_code == 200, admin_view.text
            assert admin_view.json()["status"] == "COMMITTED"

            persisted = await session.get(TicketSeries, series_id)
            assert persisted is not None
            await _record_api_tickets(session, series=persisted, buyer=buyer)
            now = datetime.now(timezone.utc)
            persisted.sales_end_at = now - timedelta(minutes=2)
            persisted.draw_at = now - timedelta(minutes=1)
            await session.commit()

            close = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/close",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "phase6-api-close-01"},
            )
            invalid_reveal = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/draw/run",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "phase6-api-invalid-reveal-1"},
                json={"seed_reveal": chr(0) * 16},
            )
            run = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/draw/run",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "phase6-api-run-0001"},
                json={"seed_reveal": seed},
            )
            prizes = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/draw/post-prizes",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "phase6-api-prizes-1"},
            )
            publish = await client.post(
                f"/api/v1/admin/ticket-series/{series_id}/draw/publish",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "phase6-api-publish1"},
            )
            public = await client.get(f"/api/v1/results/{series_id}")
            first_candidates = await client.get(f"/api/v1/results/{series_id}/candidates?limit=1")
            second_candidates = await client.get(f"/api/v1/results/{series_id}/candidates?after_serial_number=1&limit=1")
            listing = await client.get("/api/v1/results")

        assert close.status_code == 200, close.text
        assert close.json()["status"] == "CLOSED"
        assert invalid_reveal.status_code == 422, invalid_reveal.text
        assert run.status_code == 200, run.text
        assert prizes.status_code == 200, prizes.text
        assert publish.status_code == 200, publish.text
        assert public.status_code == 200, public.text
        assert public.json()["seed_reveal"] == seed
        assert "eligible_ticket_serial_numbers" not in public.json()
        assert first_candidates.status_code == 200, first_candidates.text
        assert first_candidates.json()["ticket_serial_numbers"] == [1]
        assert first_candidates.json()["next_after_serial_number"] == 1
        assert second_candidates.status_code == 200, second_candidates.text
        assert second_candidates.json()["ticket_serial_numbers"] == [2]
        assert second_candidates.json()["next_after_serial_number"] is None
        assert "owner_user_id" not in public.json()["winners"][0]
        assert listing.status_code == 200
        assert [item["series_id"] for item in listing.json()] == [str(series_id)]
    finally:
        app.dependency_overrides.clear()
