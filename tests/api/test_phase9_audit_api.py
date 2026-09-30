"""Phase 9 administrator audit metadata access and pagination boundaries."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import PageWindow
from app.core.permissions import RoleName
from app.core.security import create_access_token
from app.database import get_session
from app.main import app
from app.models.audit_log import AuditLog
from app.models.user import Role, User


def _headers(user: User, *, claim_admin: bool = False) -> dict[str, str]:
    claimed_roles = [RoleName.ADMIN.value] if claim_admin else []
    return {"Authorization": f"Bearer {create_access_token(str(user.id), claimed_roles)}"}


@pytest.mark.asyncio
async def test_admin_audit_is_bounded_filterable_and_redacted(session: AsyncSession) -> None:
    admin = User(
        email=f"audit-admin-{uuid4()}@example.test",
        password_hash="test-only-hash",
        roles=[Role(name=RoleName.ADMIN.value)],
    )
    buyer = User(email=f"audit-buyer-{uuid4()}@example.test", password_hash="test-only-hash")
    session.add_all([admin, buyer])
    await session.flush()

    now = datetime.now(timezone.utc)
    older = AuditLog(
        actor_user_id=buyer.id, entity_type="order", entity_id=uuid4(),
        action="created", created_at=now - timedelta(minutes=2),
        before_state={"secret": "hidden-before"},
        after_state={"secret": "hidden-after"}, reason="hidden-reason",
    )
    newer = AuditLog(
        actor_user_id=admin.id, entity_type="withdrawal", entity_id=uuid4(),
        action="reviewed", created_at=now - timedelta(minutes=1),
        before_state={"secret": "hidden-before"},
        after_state={"secret": "hidden-after"}, reason="hidden-reason",
    )
    session.add_all([older, newer])
    await session.commit()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            anonymous = await client.get("/api/v1/admin/audit")
            non_admin = await client.get("/api/v1/admin/audit", headers=_headers(buyer))
            forged_claim = await client.get(
                "/api/v1/admin/audit", headers=_headers(buyer, claim_admin=True)
            )
            listed = await client.get(
                "/api/v1/admin/audit?limit=1", headers=_headers(admin)
            )
            second = await client.get(
                "/api/v1/admin/audit?limit=1&offset=1", headers=_headers(admin)
            )
            filtered = await client.get(
                "/api/v1/admin/audit",
                params={
                    "actor_user_id": str(admin.id), "entity_id": str(newer.entity_id),
                    "entity_type": "withdrawal", "action": "reviewed",
                },
                headers=_headers(admin),
            )
            no_match = await client.get(
                "/api/v1/admin/audit?entity_type=does-not-exist", headers=_headers(admin)
            )
            bad_limit = await client.get(
                "/api/v1/admin/audit?limit=201", headers=_headers(admin)
            )
            bad_offset = await client.get(
                "/api/v1/admin/audit?offset=-1", headers=_headers(admin)
            )
    finally:
        app.dependency_overrides.clear()

    assert anonymous.status_code == 401
    assert non_admin.status_code == 403
    assert forged_claim.status_code == 403
    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "no-store"
    assert [item["id"] for item in listed.json()] == [str(newer.id)]
    assert [item["id"] for item in second.json()] == [str(older.id)]
    assert [item["id"] for item in filtered.json()] == [str(newer.id)]
    assert no_match.json() == []
    assert set(listed.json()[0]) == {
        "id", "actor_user_id", "entity_type", "entity_id", "action", "created_at"
    }
    assert "hidden-" not in listed.text
    assert bad_limit.status_code == 422
    assert bad_offset.status_code == 422


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(0, 0), (201, 0), (True, 0), (1.5, 0), (1, -1), (1, 1_000_001), (1, False)],
)
def test_page_window_rejects_invalid_bounds(limit: object, offset: object) -> None:
    with pytest.raises(ValueError):
        PageWindow(limit=limit, offset=offset)
