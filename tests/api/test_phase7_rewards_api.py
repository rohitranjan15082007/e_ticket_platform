"""Marketing RBAC, user referral, and evidence-backed affiliate API checks."""

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
from app.models.user import Role, User


async def _user(session: AsyncSession, *, admin: bool = False) -> User:
    user = User(
        email=f"phase7-reward-api-{uuid4()}@example.test",
        password_hash="test-only-hash",
        roles=[Role(name=RoleName.ADMIN.value)] if admin else [],
    )
    session.add(user)
    await session.commit()
    return user


def _headers(user: User, *, admin: bool = False, key: str | None = None) -> dict[str, str]:
    roles = [RoleName.ADMIN.value] if admin else []
    headers = {"Authorization": f"Bearer {create_access_token(str(user.id), roles)}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@pytest.mark.asyncio
async def test_referral_api_claim_and_admin_program_boundaries(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    referrer = await _user(session)
    referred = await _user(session)

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            denied = await client.post(
                "/api/v1/admin/marketing/referral-programs",
                headers=_headers(referred, key="referral-api-denied-001"), json={},
            )
            created = await client.post(
                "/api/v1/admin/marketing/referral-programs",
                headers=_headers(admin, admin=True, key="referral-api-program-001"),
                json={
                    "name": "First order",
                    "referrer_reward_paise": 120,
                    "referred_reward_paise": 80,
                    "minimum_order_paise": 1_000,
                },
            )
            activated = await client.post(
                f"/api/v1/admin/marketing/referral-programs/{created.json()['id']}/activate",
                headers=_headers(admin, admin=True, key="referral-api-activate-001"),
            )
            profile = await client.post(
                "/api/v1/referrals/profile",
                headers=_headers(referrer, key="referral-api-profile-001"),
            )
            claim = await client.post(
                "/api/v1/referrals/claim",
                headers=_headers(referred, key="referral-api-claim-001"),
                json={"referral_code": profile.json()["code"].lower()},
            )
            own = await client.get("/api/v1/referrals/claim", headers=_headers(referred))
            rewards_denied = await client.get(
                "/api/v1/admin/marketing/referral-rewards", headers=_headers(referred)
            )
            disabled = await client.post(
                f"/api/v1/admin/marketing/referral-programs/{created.json()['id']}/disable",
                headers=_headers(admin, admin=True, key="referral-api-disable-001"),
                json={"reason": "Campaign completed"},
            )
        assert denied.status_code == 403
        assert created.status_code == 201, created.text
        assert activated.status_code == 200, activated.text
        assert profile.status_code == 201, profile.text
        assert claim.status_code == 201, claim.text
        assert own.status_code == 200 and own.json()["id"] == claim.json()["id"]
        assert rewards_denied.status_code == 403
        assert disabled.status_code == 200 and disabled.json()["status"] == "DISABLED"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_cashback_and_affiliate_admin_api_creates_pending_conversion(session: AsyncSession) -> None:
    admin = await _user(session, admin=True)
    owner = await _user(session)
    buyer = await _user(session)
    now = datetime.now(timezone.utc)
    order = Order(
        buyer_user_id=buyer.id, status=OrderStatus.PAID,
        delivery_status=DeliveryStatus.PENDING,
        subtotal_paise=3_000, discount_paise=0, total_paise=3_000,
        currency="INR", expires_at=now + timedelta(minutes=15),
        settlement_reference_id=uuid4(), settled_at=now,
    )
    session.add(order)
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            campaign = await client.post(
                "/api/v1/admin/marketing/cashback-campaigns",
                headers=_headers(admin, admin=True, key="cashback-api-create-001"),
                json={
                    "code": "BACK100", "name": "One rupee", "reward_type": "FIXED_PAISE",
                    "fixed_reward_paise": 100, "minimum_order_paise": 1_000,
                },
            )
            campaign_active = await client.post(
                f"/api/v1/admin/marketing/cashback-campaigns/{campaign.json()['id']}/activate",
                headers=_headers(admin, admin=True, key="cashback-api-activate-001"),
            )
            affiliate = await client.post(
                "/api/v1/admin/marketing/affiliates",
                headers=_headers(admin, admin=True, key="affiliate-api-create-001"),
                json={
                    "code": "PARTNER7", "display_name": "Partner Seven",
                    "owner_user_id": str(owner.id), "commission_type": "FIXED_PAISE",
                    "fixed_commission_paise": 150, "minimum_order_paise": 1_000,
                },
            )
            affiliate_active = await client.post(
                f"/api/v1/admin/marketing/affiliates/{affiliate.json()['id']}/activate",
                headers=_headers(admin, admin=True, key="affiliate-api-activate-001"),
            )
            converted = await client.post(
                "/api/v1/admin/marketing/affiliate-conversions",
                headers=_headers(admin, admin=True, key="affiliate-api-convert-001"),
                json={
                    "affiliate_id": affiliate.json()["id"], "order_id": str(order.id),
                    "evidence_reference": "signed-report/api-001",
                },
            )
            replay = await client.post(
                "/api/v1/admin/marketing/affiliate-conversions",
                headers=_headers(admin, admin=True, key="affiliate-api-convert-001"),
                json={
                    "affiliate_id": affiliate.json()["id"], "order_id": str(order.id),
                    "evidence_reference": "signed-report/api-001",
                },
            )
            voided = await client.post(
                f"/api/v1/admin/marketing/affiliate-conversions/{converted.json()['id']}/void",
                headers=_headers(admin, admin=True, key="affiliate-api-void-001"),
                json={"reason": "Evidence rejected by reviewer"},
            )
            suspended = await client.post(
                f"/api/v1/admin/marketing/affiliates/{affiliate.json()['id']}/suspend",
                headers=_headers(admin, admin=True, key="affiliate-api-suspend-001"),
                json={"reason": "Partner review"},
            )
            campaign_disabled = await client.post(
                f"/api/v1/admin/marketing/cashback-campaigns/{campaign.json()['id']}/disable",
                headers=_headers(admin, admin=True, key="cashback-api-disable-001"),
                json={"reason": "Campaign completed"},
            )
        assert campaign.status_code == 201, campaign.text
        assert campaign_active.status_code == 200, campaign_active.text
        assert affiliate.status_code == 201, affiliate.text
        assert affiliate_active.status_code == 200, affiliate_active.text
        assert converted.status_code == 201, converted.text
        assert converted.json()["commission"]["status"] == "PENDING_REVIEW"
        assert replay.status_code == 201 and replay.json()["id"] == converted.json()["id"]
        assert voided.status_code == 200 and voided.json()["commission"]["status"] == "VOIDED"
        assert suspended.status_code == 200 and suspended.json()["status"] == "SUSPENDED"
        assert campaign_disabled.status_code == 200 and campaign_disabled.json()["status"] == "DISABLED"
    finally:
        app.dependency_overrides.clear()
