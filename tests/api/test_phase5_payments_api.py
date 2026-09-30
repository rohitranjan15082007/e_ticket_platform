"""Authorization and evidence-bound checks for the Phase 5 payment API."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import RoleName
from app.core.security import create_access_token
from app.database import get_session
from app.main import app
from app.models.order import DeliveryStatus, Order, OrderStatus
from app.models.user import Role, User


async def _user(session: AsyncSession, *, label: str, admin: bool = False) -> User:
    roles: list[Role] = []
    if admin:
        role = await session.scalar(select(Role).where(Role.name == RoleName.ADMIN.value))
        if role is None:
            role = Role(name=RoleName.ADMIN.value, description="Phase 5 API test administrator")
        roles = [role]
    user = User(
        email=f"phase5-api-{label}-{uuid4()}@example.test",
        password_hash="test-only-hash",
        roles=roles,
    )
    session.add(user)
    await session.commit()
    return user


def _authorization(user: User, *, admin: bool = False) -> dict[str, str]:
    roles = [RoleName.ADMIN.value] if admin else []
    return {"Authorization": f"Bearer {create_access_token(str(user.id), roles)}"}


@pytest.mark.asyncio
async def test_manual_payment_api_keeps_proof_in_review_until_admin_approval(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TICKET_MANUAL_UPI_ENABLED", "true")
    get_settings.cache_clear()
    buyer = await _user(session, label="buyer")
    outsider = await _user(session, label="outsider")
    admin = await _user(session, label="admin", admin=True)
    order = Order(
        buyer_user_id=buyer.id,
        status=OrderStatus.PENDING_PAYMENT,
        delivery_status=DeliveryStatus.NOT_STARTED,
        total_paise=12_345,
        currency="INR",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    session.add(order)
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            forbidden_destination = await client.post(
                "/api/v1/admin/manual-upi/destinations",
                headers={**_authorization(buyer), "Idempotency-Key": "phase5-api-destination-buyer-001"},
                json={
                    "display_label": "Test merchant",
                    "upi_id": "test-merchant@upi",
                    "approval_evidence_reference": "ops-approved-phase5-api-001",
                },
            )
            destination = await client.post(
                "/api/v1/admin/manual-upi/destinations",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "phase5-api-destination-admin-001"},
                json={
                    "display_label": "Test merchant",
                    "upi_id": "test-merchant@upi",
                    "approval_evidence_reference": "ops-approved-phase5-api-001",
                    "instructions": {"support": "Save the UTR for review."},
                },
            )
            started = await client.post(
                f"/api/v1/orders/{order.id}/payments",
                headers={**_authorization(buyer), "Idempotency-Key": "phase5-api-start-manual-001"},
                json={"method": "MANUAL_UPI"},
            )
            assert started.status_code == 200, started.text
            attempt_id = started.json()["payment"]["id"]
            outsider_read = await client.get(
                f"/api/v1/payments/{attempt_id}", headers=_authorization(outsider)
            )
            proof = await client.post(
                f"/api/v1/payments/{attempt_id}/manual-proof",
                headers={**_authorization(buyer), "Idempotency-Key": "phase5-api-proof-001"},
                json={
                    "utr": "PHASE5-API-UTR-001",
                    "proof_reference": "storage://phase5/api-proof.png",
                    "submitted_amount_paise": 12_345,
                    "submitted_currency": "INR",
                },
            )
            review_queue = await client.get(
                "/api/v1/admin/manual-upi/review", headers=_authorization(admin, admin=True)
            )
            approval = await client.post(
                f"/api/v1/admin/manual-upi/{attempt_id}/review",
                headers={**_authorization(admin, admin=True), "Idempotency-Key": "phase5-api-review-001"},
                json={
                    "decision": "APPROVE",
                    "review_note": "Independent bank evidence confirms the exact frozen amount.",
                },
            )

        assert forbidden_destination.status_code == 403
        assert destination.status_code == 201, destination.text
        assert destination.json()["qr_reference"] is None
        assert started.json()["payment"]["status"] == "AWAITING_PAYMENT"
        assert started.json()["payment"]["method_data_snapshot"]["proof_is_not_settlement"] is True
        assert outsider_read.status_code == 422
        assert proof.status_code == 200, proof.text
        assert proof.json()["review_required"] is True
        assert proof.json()["payment"]["status"] == "UNDER_REVIEW"
        assert review_queue.status_code == 200, review_queue.text
        assert len(review_queue.json()) == 1
        assert approval.status_code == 200, approval.text
        assert approval.json()["review_required"] is False
        assert approval.json()["payment"]["status"] == "SUCCEEDED"
        assert approval.json()["settlement_id"] is not None
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
