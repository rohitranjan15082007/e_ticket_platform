"""One-time admin bootstrap and inert development seed invariants."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RoleName
from app.core.security import verify_password
from app.exceptions import AuthorizationError, ConflictError
from app.models.audit_log import AuditLog
from app.models.ticket_series import TicketSeries, TicketSeriesStatus
from app.models.user import IdempotencyRecord, Role, User
from app.services.admin_bootstrap_service import AdminBootstrapService
from app.services.demo_seed_service import DEMO_NAME, DemoSeedService
from scripts import create_admin, seed_demo_data


@pytest.mark.asyncio
async def test_first_admin_bootstrap_is_audited_idempotent_and_one_time(session: AsyncSession) -> None:
    service = AdminBootstrapService(session)
    email = f"first-{uuid4()}@example.test"
    password = "A-long-test-password-2026!"
    first = await service.create_first_admin(
        email=email, password=password, idempotency_key="first-admin-test-key", commit=True,
    )
    replay = await service.create_first_admin(
        email=email, password=password, idempotency_key="first-admin-test-key", commit=True,
    )
    user = await session.get(User, first.user_id)
    assert user is not None
    await session.refresh(user, attribute_names=["roles"])
    assert {role.name for role in user.roles} == {RoleName.USER.value, RoleName.ADMIN.value}
    assert verify_password(password, user.password_hash)
    assert user.password_hash != password
    assert not first.replayed and replay.replayed
    assert replay.user_id == first.user_id
    assert await session.scalar(select(func.count()).select_from(User)) == 1
    assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
    audits = (await session.scalars(select(AuditLog))).all()
    assert len(audits) == 1 and audits[0].action == "FIRST_ADMIN_BOOTSTRAPPED"
    assert password not in str(audits[0].after_state)

    with pytest.raises(ConflictError):
        await service.create_first_admin(
            email=email, password="Another-long-password-2026!",
            idempotency_key="first-admin-test-key", commit=True,
        )
    with pytest.raises(ConflictError):
        await service.create_first_admin(
            email=f"second-{uuid4()}@example.test", password=password,
            idempotency_key="different-admin-key", commit=True,
        )


@pytest.mark.asyncio
async def test_demo_seed_is_draft_only_idempotent_and_admin_gated(session: AsyncSession) -> None:
    admin = User(
        email=f"seed-admin-{uuid4()}@example.test", password_hash="test-only",
        roles=[Role(name=RoleName.ADMIN.value)],
    )
    buyer = User(email=f"seed-buyer-{uuid4()}@example.test", password_hash="test-only")
    session.add_all([admin, buyer])
    await session.commit()
    service = DemoSeedService(session)

    with pytest.raises(AuthorizationError):
        await service.seed_draft_series(admin_user_id=buyer.id, commit=True)
    first = await service.seed_draft_series(admin_user_id=admin.id, commit=True)
    replay = await service.seed_draft_series(admin_user_id=admin.id, commit=True)
    assert not first.existed and replay.existed and first.series_id == replay.series_id
    series = await session.get(TicketSeries, first.series_id)
    assert series is not None and series.name == DEMO_NAME
    assert series.status == TicketSeriesStatus.DRAFT
    assert await session.scalar(select(func.count()).select_from(TicketSeries)) == 1
    assert await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == "TICKET_SERIES_CREATED")
    ) == 1


@pytest.mark.asyncio
async def test_demo_seed_rejects_production_even_with_admin(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = User(
        email=f"seed-prod-{uuid4()}@example.test", password_hash="test-only",
        roles=[Role(name=RoleName.ADMIN.value)],
    )
    session.add(admin)
    await session.commit()
    monkeypatch.setattr("app.services.demo_seed_service.get_settings", lambda: SimpleNamespace(app_env="production"))
    with pytest.raises(ValueError, match="development"):
        await DemoSeedService(session).seed_draft_series(admin_user_id=admin.id, commit=True)
    assert await session.scalar(select(func.count()).select_from(TicketSeries)) == 0


def test_maintenance_clis_require_explicit_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TICKET_DATABASE_URL", raising=False)
    monkeypatch.delenv("TICKET_APP_ENV", raising=False)
    assert create_admin.main(["--email", "a@example.test", "--idempotency-key", "safe-key-123"]) == 2
    assert seed_demo_data.main(["--admin-email", "a@example.test"]) == 2
