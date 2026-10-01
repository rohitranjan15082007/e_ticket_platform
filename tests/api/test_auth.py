"""End-to-end API tests for Phase 1 authentication routes."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import rate_limit
from app.database import get_session
from app.main import app


class FakeAuthRedis:
    """In-memory stand-in for the one atomic Redis script call."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.calls: list[tuple[str, ...]] = []
        self.fail = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, _type, _value, _traceback) -> None:
        return None

    async def eval(self, _script: str, number_of_keys: int, *keys_and_arguments: object) -> int:
        if self.fail:
            raise ConnectionError("Redis unavailable")
        keys = tuple(str(value) for value in keys_and_arguments[:number_of_keys])
        arguments = keys_and_arguments[number_of_keys:]
        self.calls.append(keys)
        blocked_ms = 0
        for index, key in enumerate(keys):
            self.counts[key] = self.counts.get(key, 0) + 1
            maximum = int(arguments[index * 2])
            if self.counts[key] > maximum:
                blocked_ms = max(blocked_ms, int(arguments[index * 2 + 1]))
        return blocked_ms


@pytest.fixture
def fake_auth_redis(monkeypatch: pytest.MonkeyPatch) -> FakeAuthRedis:
    fake = FakeAuthRedis()
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setattr(rate_limit.Redis, "from_url", lambda *_args, **_kwargs: fake)
    return fake


@pytest.mark.asyncio
async def test_register_then_login_through_api(
    session: AsyncSession, fake_auth_redis: FakeAuthRedis,
) -> None:
    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            registration = await client.post(
                "/api/v1/auth/register",
                headers={"Idempotency-Key": "api-register-key-001"},
                json={
                    "email": "api-user@example.com",
                    "password": "correct-horse-battery-staple",
                    "full_name": "API User",
                },
            )
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": "api-user@example.com", "password": "correct-horse-battery-staple"},
            )

        assert registration.status_code == 201
        assert registration.json()["roles"] == ["user"]
        assert login.status_code == 200
        assert login.json()["token"]["token_type"] == "bearer"
        stored_keys = " ".join(key for call in fake_auth_redis.calls for key in call)
        for private_value in (
            "api-user@example.com", "correct-horse-battery-staple",
            "127.0.0.1", get_settings().jwt_secret,
        ):
            assert private_value not in stored_keys
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_login_rate_limit_is_shared_by_account_but_not_other_accounts(
    session: AsyncSession, fake_auth_redis: FakeAuthRedis, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rate_limit, "LOGIN_ACCOUNT_LIMIT", 2)

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def login(email: str):
                return await client.post(
                    "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
                )

            assert (await login("first@example.com")).status_code == 401
            assert (await login("first@example.com")).status_code == 401
            limited = await login("first@example.com")
            assert limited.status_code == 429
            assert limited.json()["code"] == "AUTH_RATE_LIMITED"
            assert (await login("second@example.com")).status_code == 401
        assert len(fake_auth_redis.calls) == 4
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_rate_limit_uses_peer_not_spoofed_forwarding_header(
    session: AsyncSession, fake_auth_redis: FakeAuthRedis, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setattr(rate_limit, "FALLBACK_REGISTER_CLIENT_LIMIT", 2)

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.5", 6000)), base_url="http://test"
        ) as client:
            for index in range(2):
                response = await client.post(
                    "/api/v1/auth/register",
                    headers={"Idempotency-Key": f"register-key-{index:03d}"},
                    json={"email": f"user{index}@example.com", "password": "long-enough-password"},
                )
                assert response.status_code == 201
            blocked = await client.post(
                "/api/v1/auth/register",
                headers={"Idempotency-Key": "register-key-003", "X-Forwarded-For": "198.51.100.9"},
                json={"email": "user3@example.com", "password": "long-enough-password"},
            )
            assert blocked.status_code == 429
            assert blocked.json()["code"] == "AUTH_RATE_LIMITED"
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.6", 6000)), base_url="http://test"
        ) as another_client:
            allowed = await another_client.post(
                "/api/v1/auth/register", headers={"Idempotency-Key": "register-key-004"},
                json={"email": "user4@example.com", "password": "long-enough-password"},
            )
            assert allowed.status_code == 201
        stored_keys = " ".join(key for call in fake_auth_redis.calls for key in call)
        assert "203.0.113.5" not in stored_keys
        assert "user3@example.com" not in stored_keys
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_vercel_rate_limit_uses_sanitized_client_ip(
    session: AsyncSession, fake_auth_redis: FakeAuthRedis, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(rate_limit, "REGISTER_CLIENT_LIMIT", 1)

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("192.0.2.50", 6000)), base_url="http://test"
        ) as client:
            for index, address in enumerate(("203.0.113.1", "203.0.113.2")):
                response = await client.post(
                    "/api/v1/auth/register",
                    headers={"Idempotency-Key": f"vercel-register-{index}", "X-Forwarded-For": address},
                    json={"email": f"vercel{index}@example.com", "password": "long-enough-password"},
                )
                assert response.status_code == 201
            limited = await client.post(
                "/api/v1/auth/register",
                headers={"Idempotency-Key": "vercel-register-2", "X-Forwarded-For": "203.0.113.1"},
                json={"email": "vercel2@example.com", "password": "long-enough-password"},
            )
            assert limited.status_code == 429
            invalid = await client.post(
                "/api/v1/auth/register",
                headers={"Idempotency-Key": "vercel-register-3", "X-Forwarded-For": "not-an-ip"},
                json={"email": "vercel3@example.com", "password": "long-enough-password"},
            )
            assert invalid.status_code == 503
            assert invalid.json()["code"] == "AUTH_RATE_LIMIT_UNAVAILABLE"
        stored_keys = " ".join(key for call in fake_auth_redis.calls for key in call)
        assert "203.0.113.1" not in stored_keys
        assert "192.0.2.50" not in stored_keys
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_auth_fails_closed_when_redis_is_unavailable(
    session: AsyncSession, fake_auth_redis: FakeAuthRedis,
) -> None:
    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        fake_auth_redis.fail = True
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            registration = await client.post(
                "/api/v1/auth/register", headers={"Idempotency-Key": "redis-outage-key"},
                json={"email": "outage@example.com", "password": "long-enough-password"},
            )
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": "outage@example.com", "password": "long-enough-password"},
            )
            assert registration.status_code == 503
            assert login.status_code == 503
            assert registration.json()["code"] == "AUTH_RATE_LIMIT_UNAVAILABLE"
            fake_auth_redis.fail = False
            registered_after_recovery = await client.post(
                "/api/v1/auth/register", headers={"Idempotency-Key": "redis-outage-key"},
                json={"email": "outage@example.com", "password": "long-enough-password"},
            )
            assert registered_after_recovery.status_code == 201
    finally:
        app.dependency_overrides.clear()
