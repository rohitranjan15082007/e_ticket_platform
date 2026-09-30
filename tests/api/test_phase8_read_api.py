"""Phase 8 owner reads and administrator dashboard boundaries."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.core.security import create_access_token
from app.database import get_session
from app.main import app
from app.models.order import Order, OrderStatus
from app.models.payment import PaymentAttempt, PaymentMethod
from app.models.user import IdempotencyRecord, Role, User
from app.models.withdrawal import PaymentDestination, PaymentDestinationStatus


async def _user(session: AsyncSession, *, admin: bool = False) -> User:
    user = User(
        email=f"phase8-{uuid4()}@example.test",
        password_hash="test-only-hash",
        roles=[Role(name=RoleName.ADMIN.value)] if admin else [],
    )
    session.add(user)
    await session.commit()
    return user


def _headers(user: User, *, admin: bool = False, key: str | None = None) -> dict[str, str]:
    roles = [RoleName.ADMIN.value] if admin else []
    result = {"Authorization": f"Bearer {create_access_token(str(user.id), roles)}"}
    if key:
        result["Idempotency-Key"] = key
    return result


@pytest.mark.asyncio
async def test_wallet_and_destination_reads_are_owner_scoped(session: AsyncSession) -> None:
    owner = await _user(session)
    other = await _user(session)
    destination = PaymentDestination(
        user_id=owner.id, provider_namespace="upi", display_label="Verified UPI",
        destination_data={"upi_id": "owner@example.test"},
        status=PaymentDestinationStatus.VERIFIED,
        verification_method="independent-review",
        verification_evidence_reference="review-001",
        verified_at=datetime.now(timezone.utc),
    )
    session.add(destination)
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            empty = await client.get("/api/v1/wallet", headers=_headers(owner))
            created = await client.post(
                "/api/v1/wallet/provision",
                headers=_headers(owner, key="phase8-wallet-provision-001"),
            )
            replay = await client.post(
                "/api/v1/wallet/provision",
                headers=_headers(owner, key="phase8-wallet-provision-001"),
            )
            balance = await client.get("/api/v1/wallet", headers=_headers(owner))
            entries = await client.get("/api/v1/wallet/transactions", headers=_headers(owner))
            own_destinations = await client.get(
                "/api/v1/withdrawals/destinations", headers=_headers(owner)
            )
            hidden_destinations = await client.get(
                "/api/v1/withdrawals/destinations", headers=_headers(other)
            )
            withdrawals = await client.get("/api/v1/withdrawals", headers=_headers(owner))
        assert empty.status_code == 200 and empty.json() is None
        assert created.status_code == 201, created.text
        assert replay.status_code == 201 and replay.json()["id"] == created.json()["id"]
        assert balance.status_code == 200 and balance.json()["available_paise"] == 0
        assert entries.status_code == 200 and entries.json() == []
        assert own_destinations.status_code == 200 and own_destinations.json()[0]["id"] == str(destination.id)
        assert hidden_destinations.status_code == 200 and hidden_destinations.json() == []
        assert withdrawals.status_code == 200 and withdrawals.json() == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_admin_dashboard_and_queues_require_admin_role(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    buyer = await _user(session)

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            denied = await client.get("/api/v1/admin/dashboard/summary", headers=_headers(buyer))
            summary = await client.get(
                "/api/v1/admin/dashboard/summary", headers=_headers(admin, admin=True)
            )
            series = await client.get(
                "/api/v1/admin/ticket-series", headers=_headers(admin, admin=True)
            )
            withdrawals = await client.get(
                "/api/v1/admin/withdrawals", headers=_headers(admin, admin=True)
            )
            disputes = await client.get(
                "/api/v1/admin/disputes", headers=_headers(admin, admin=True)
            )
        assert denied.status_code == 403
        assert summary.status_code == 200, summary.text
        assert summary.json()["total_users"] == 2
        assert summary.json()["allocated_revenue_paise"] == 0
        assert series.status_code == 200 and series.json() == []
        assert withdrawals.status_code == 200 and withdrawals.json() == []
        assert disputes.status_code == 200 and disputes.json() == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_checkout_state_is_owner_only_and_never_asserts_payment(session: AsyncSession) -> None:
    owner = await _user(session)
    other = await _user(session)
    order = Order(
        buyer_user_id=owner.id, total_paise=12300,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    session.add(order)
    await session.commit()
    record = IdempotencyRecord(
        actor_scope=f"user:{owner.id}:payment.create", idempotency_key="phase8-checkout-read-001",
        request_fingerprint="a" * 64, resource_type="payment_attempt", status_code=201,
    )
    session.add(record)
    await session.flush()
    attempt = PaymentAttempt(
        order_id=order.id, buyer_user_id=owner.id, method=PaymentMethod.MANUAL_UPI,
        provider_namespace="manual_upi", order_amount_paise=12300, order_currency="INR",
        provider_amount=12300, provider_currency="INR", method_data_snapshot={},
        merchant_reference=f"phase8-{uuid4()}", idempotency_record_id=record.id,
        expires_at=order.expires_at,
    )
    order.status = OrderStatus.AWAITING_PAYMENT
    session.add(attempt)
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            own = await client.get(
                f"/api/v1/orders/{order.id}/checkout-state", headers=_headers(owner),
            )
            other_response = await client.get(
                f"/api/v1/orders/{order.id}/checkout-state", headers=_headers(other),
            )
        assert own.status_code == 200, own.text
        assert own.json() == {
            "order_id": str(order.id), "order_status": "AWAITING_PAYMENT",
            "payment_attempt_id": str(attempt.id), "p2p_match_id": None,
        }
        assert other_response.status_code == 422
    finally:
        app.dependency_overrides.clear()
