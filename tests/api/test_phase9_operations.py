"""Production fail-closed settings and dependency-aware operational probes."""

import importlib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from app.config import Settings
from app.main import app


def production_settings(**overrides: object) -> Settings:
    values = {
        "_env_file": None,
        "app_env": "production",
        "debug": False,
        "jwt_secret": "phase9-test-only-unique-secret-not-for-use-in-production",
        "database_url": "postgresql+asyncpg://ticket_user:nonexample-secret@db.example.test/tickets",
        "allowed_origins": ["https://tickets.example.test"],
    }
    values.update(overrides)
    return Settings(**values)


def test_production_settings_reject_example_credentials_and_insecure_origins() -> None:
    assert production_settings().app_env == "production"
    for overrides, message in [
        ({"jwt_secret": "development-only-change-before-production"}, "JWT_SECRET"),
        ({"jwt_secret": "replace-with-a-random-secret-of-at-least-32-characters"}, "JWT_SECRET"),
        ({"debug": True}, "DEBUG"),
        ({"database_url": "postgresql+asyncpg://ticket_user:ticket_password@db/tickets"}, "DATABASE_URL"),
        ({"database_url": "sqlite+aiosqlite:///local.db"}, "DATABASE_URL"),
        ({"allowed_origins": ["http://tickets.example.test"]}, "ALLOWED_ORIGINS"),
        ({"allowed_origins": ["*"]}, "ALLOWED_ORIGINS"),
        ({"allowed_origins": ["https://*.example.test"]}, "ALLOWED_ORIGINS"),
    ]:
        with pytest.raises(PydanticValidationError, match=message):
            production_settings(**overrides)


def test_ready_probes_both_dependencies_without_leaking_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    main = importlib.import_module("app.main")

    async def connected() -> None:
        return None

    async def unavailable() -> None:
        raise RuntimeError("secret internal connection information")

    monkeypatch.setattr(main, "verify_database_connection", connected)
    monkeypatch.setattr(main, "verify_redis_connection", connected)
    with TestClient(app) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}
        monkeypatch.setattr(main, "verify_redis_connection", unavailable)
        unavailable_response = client.get("/ready")
    assert unavailable_response.status_code == 503
    assert unavailable_response.json()["checks"] == {"database": "ok", "redis": "unavailable"}
    assert "secret internal" not in unavailable_response.text
    assert unavailable_response.headers["cache-control"] == "no-store"


def test_browser_and_api_responses_have_defensive_headers() -> None:
    with TestClient(app) as client:
        page = client.get("/")
        unauthorized = client.get("/api/v1/auth/me")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert unauthorized.status_code == 401
    assert unauthorized.headers["cache-control"] == "no-store"
