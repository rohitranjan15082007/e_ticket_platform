"""Tests for registration identity idempotency and default RBAC role."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ConflictError
from app.models.user import IdempotencyRecord, User
from app.schemas.auth import RegisterRequest
from app.services.auth_service import AuthService


@pytest.mark.asyncio
async def test_registration_is_idempotent_and_assigns_user_role(session: AsyncSession) -> None:
    service = AuthService(session)
    payload = RegisterRequest(
        email="person@example.com", password="correct-horse-battery-staple", full_name="Test Person"
    )

    first = await service.register(payload, "register-person-001")
    retried = await service.register(payload, "register-person-001")

    assert first.id == retried.id
    assert [role.name for role in retried.roles] == ["user"]
    assert await session.scalar(select(func.count()).select_from(User)) == 1
    assert await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


@pytest.mark.asyncio
async def test_reused_registration_key_with_changed_request_is_rejected(session: AsyncSession) -> None:
    service = AuthService(session)
    await service.register(
        RegisterRequest(email="person@example.com", password="correct-horse-battery-staple"),
        "register-person-002",
    )

    with pytest.raises(ConflictError, match="IDEMPOTENCY_CONFLICT"):
        await service.register(
            RegisterRequest(email="person@example.com", password="different-password-value"),
            "register-person-002",
        )
