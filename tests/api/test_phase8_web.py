"""Phase 8 pages and assets are same-origin, complete, and CSP-protected."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.web import PAGES
from app.main import app


@pytest.mark.asyncio
async def test_every_phase8_page_serves_a_real_shell() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in PAGES:
            response = await client.get(path)
            assert response.status_code == 200, path
            assert 'id="page-content"' in response.text
            assert '/assets/js/app.js' in response.text
            assert 'href="#main"' in response.text
            assert "script-src 'self'" in response.headers["content-security-policy"]
            assert "nosniff" == response.headers["x-content-type-options"]


@pytest.mark.asyncio
async def test_frontend_modules_and_styles_are_served() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in (
            "/assets/css/base.css", "/assets/css/user.css", "/assets/css/admin.css",
            "/assets/js/app.js", "/assets/js/api.js", "/assets/js/catalog.js",
            "/assets/js/checkout.js", "/assets/js/user.js", "/assets/js/wallet.js",
            "/assets/js/admin.js",
        ):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert "PLANNED:" not in response.text, path


@pytest.mark.asyncio
async def test_admin_frontend_maps_the_complete_read_only_contract() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/admin")
        script = await client.get("/assets/js/admin.js")

    assert page.status_code == 200
    assert '/assets/css/admin-v2.css' in page.text
    assert script.status_code == 200
    for counter in (
        "total_users", "open_series", "closed_series", "tickets_sold",
        "open_tickets_remaining", "pending_orders", "pending_manual_proofs",
        "active_p2p_matches", "open_disputes", "unresolved_withdrawals",
        "pending_referral_rewards", "pending_cashback_rewards",
        "pending_affiliate_commissions", "allocated_revenue_paise",
    ):
        assert f"data.{counter}" in script.text
    for resource in (
        "coupons", "referral-programs", "cashback-campaigns", "affiliates",
        "referral-rewards", "cashback-rewards", "affiliate-conversions",
        "affiliate-commissions",
    ):
        assert f'"{resource}"' in script.text
    assert "post(" not in script.text


@pytest.mark.asyncio
async def test_payment_demo_is_clearly_labeled_and_has_no_payment_mutation() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/payment-demo")
        script = await client.get("/assets/js/payment_demo.js")
        styles = await client.get("/assets/css/payment_demo.css")

    assert page.status_code == 200
    assert "UI DEMO ONLY" in page.text
    assert "No payment is initiated or recorded" in page.text
    assert script.status_code == 200 and styles.status_code == 200
    assert "./api.js" in script.text
    assert "fetch(" not in script.text
    assert "post(" not in script.text
