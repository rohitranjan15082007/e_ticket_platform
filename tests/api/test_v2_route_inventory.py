"""UI route shells and scoped assets; data and policy readiness are separate."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.web import PAGES, router
from app.main import app


@pytest.mark.asyncio
async def test_proposed_product_routes_serve_shells_without_external_assets() -> None:
    record_id = uuid4()
    dynamic = (
        f"/orders/{record_id}", f"/series/{record_id}",
        f"/packages/{record_id}", f"/results/{record_id}",
        f"/tickets/{record_id}", f"/withdrawals/{record_id}",
        f"/admin/users/{record_id}", f"/admin/catalog/series/{record_id}",
        f"/admin/draws/{record_id}", f"/admin/payments/{record_id}",
        f"/admin/disputes/{record_id}",
    )
    product_pages = set(PAGES) - {"/payment-demo", "/admin/actions"}
    assert len(product_pages) + len(dynamic) == 44  # Demo and internal action console excluded.
    assert len(router.routes) == len(PAGES) + len(dynamic)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for route in (*PAGES, *dynamic):
            response = await client.get(route)
            assert response.status_code == 200, route
            assert 'id="page-content"' in response.text, route
            assert '/assets/js/app.js' in response.text, route
            assert "script-src 'self'" in response.headers["content-security-policy"], route
        for asset in (
            "/assets/css/admin-v2.css", "/assets/css/content-v2.css",
            "/assets/js/admin.js", "/assets/js/admin_actions.js",
            "/assets/js/content_v2.js", "/assets/js/utility_v2.js",
        ):
            response = await client.get(asset)
            assert response.status_code == 200, asset


@pytest.mark.asyncio
async def test_dynamic_shell_paths_validate_ids_and_catalog_kind() -> None:
    record_id = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/tickets/not-an-id")).status_code == 422
        assert (await client.get(f"/admin/catalog/other/{record_id}")).status_code == 404
