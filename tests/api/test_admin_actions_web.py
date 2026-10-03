"""The browser admin exposes every existing mutation without weakening API guards."""

import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_admin_actions_shell_and_module_are_served() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/admin/actions")
        workspace = await client.get("/assets/js/admin.js")
        actions = await client.get("/assets/js/admin_actions.js")
        api = await client.get("/assets/js/api.js")

    assert page.status_code == 200
    assert 'data-area="admin"' in page.text
    assert '/assets/css/admin-v2.css' in page.text
    assert workspace.status_code == actions.status_code == api.status_code == 200
    assert 'from "./admin_actions.js"' in workspace.text
    assert '"/admin/actions"' in workspace.text
    assert "export const put" in api.text


@pytest.mark.asyncio
async def test_admin_action_registry_exactly_matches_backend_mutations() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/assets/js/admin_actions.js")

    assert response.status_code == 200
    source = response.text
    frontend_paths = set(re.findall(r'"(/api/v1/admin/[^"`]+)"', source))
    backend_operations = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1/admin/")
        for method in operations
        if method.upper() in {"POST", "PUT"}
    }
    backend_paths = {path for _, path in backend_operations}

    assert len(backend_operations) == 34
    assert len(frontend_paths) == 34
    assert frontend_paths == backend_paths
    assert {path for method, path in backend_operations if method == "PUT"} == {
        "/api/v1/admin/ticket-series/{series_id}",
        "/api/v1/admin/ticket-packages/{package_id}",
    }
    assert source.count('method: "PUT"') == 2
    assert 'method: "POST"' in source


@pytest.mark.asyncio
async def test_admin_actions_keep_browser_and_financial_safety_guards() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        source = (await client.get("/assets/js/admin_actions.js")).text

    assert 'import {clear, el, errorMessage, post, put, showStatus}' in source
    assert 'action.method === "PUT" ? await put(' in source
    assert "await post(path, body, true)" in source
    assert "crypto.randomUUID" not in source  # centralized in api.js
    assert "confirmation" in source and "Type ${action.confirm} exactly" in source
    assert 'output.tabIndex = 0' in source
    assert 'output.setAttribute("aria-label", "Server response")' in source
    for phrase in (
        "RUN DRAW", "POST PRIZES", "REVIEW PAYMENT", "RESOLVE DISPUTE",
        "CREATE REFUND CASE", "VOID REFERRAL REWARD", "VOID CASHBACK REWARD",
        "VOID AFFILIATE CONVERSION",
    ):
        assert phrase in source
    for unsafe_dom_api in ("innerHTML", "outerHTML", "document.write", "eval("):
        assert unsafe_dom_api not in source
