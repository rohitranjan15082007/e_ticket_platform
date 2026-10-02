"""Read-only dependency preflight. Run as: python -m scripts.release_preflight.

This checks the configured PostgreSQL schema revision and Redis connectivity.
It does not verify Celery worker/beat ownership or any payment-provider flow.
Exit 0 means these two checks passed, 2 means a migration mismatch, and 3
means configuration or a dependency probe failed. No database writes occur.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.database_connection import async_database_options
from app.operations import verify_redis_connection

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE_TIMEOUT_SECONDS = 5.0


def _repository_head() -> str:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    # The ini uses a relative script_location; resolve it from this file so
    # the command also works when invoked from outside the project directory.
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise ValueError("Expected exactly one repository migration head")
    return heads[0]


async def _database_revisions(database_url: str) -> list[str]:
    engine_url, connect_args = async_database_options(database_url)
    engine = create_async_engine(engine_url, pool_pre_ping=True, connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            return sorted(str(version) for version in result.scalars().all())
    finally:
        await engine.dispose()


async def run_check() -> int:
    checks: dict[str, dict[str, object]] = {
        "migrations": {"status": "not_run"},
        "redis": {"status": "not_run"},
    }
    report: dict[str, object] = {
        "status": "fail",
        "checks": checks,
        "not_verified": ["celery_worker", "celery_beat", "payment_providers"],
    }

    try:
        settings = get_settings()
    except Exception as error:
        checks["migrations"] = {"status": "error", "error_type": type(error).__name__}
        checks["redis"] = {"status": "not_run"}
        print(json.dumps(report, sort_keys=True))
        return 3

    try:
        if make_url(settings.database_url).drivername not in {"postgresql", "postgresql+asyncpg"}:
            raise ValueError("Expected PostgreSQL URL")
        repository_head = _repository_head()
        database_revisions = await asyncio.wait_for(
            _database_revisions(settings.database_url), timeout=PROBE_TIMEOUT_SECONDS
        )
        checks["migrations"] = {
            "status": "ok" if database_revisions == [repository_head] else "mismatch",
            "repository_head": repository_head,
            "database_revisions": database_revisions,
        }
    except Exception as error:
        # Never include an exception message: drivers may embed the DSN in it.
        checks["migrations"] = {"status": "error", "error_type": type(error).__name__}

    try:
        if urlparse(settings.redis_url).scheme not in {"redis", "rediss"}:
            raise ValueError("Expected Redis protocol URL")
        await asyncio.wait_for(verify_redis_connection(), timeout=PROBE_TIMEOUT_SECONDS)
        checks["redis"] = {"status": "ok"}
    except Exception as error:
        checks["redis"] = {"status": "error", "error_type": type(error).__name__}

    statuses = {check["status"] for check in checks.values()}
    if statuses == {"ok"}:
        report["status"] = "ok"
        exit_code = 0
    elif "error" in statuses:
        exit_code = 3
    else:
        exit_code = 2
    print(json.dumps(report, sort_keys=True))
    return exit_code


def main() -> int:
    return asyncio.run(run_check())


if __name__ == "__main__":
    raise SystemExit(main())
