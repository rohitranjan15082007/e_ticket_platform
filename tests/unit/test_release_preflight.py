"""Read-only release preflight reporting and failure-boundary tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts import release_preflight


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    revisions: list[str] | Exception,
    redis_error: Exception | None = None,
) -> None:
    monkeypatch.setattr(
        release_preflight,
        "get_settings",
        lambda: SimpleNamespace(
            database_url="postgresql+asyncpg://user:secret@db.example.test/ticket",
            redis_url="rediss://user:secret@redis.example.test:6379/0",
        ),
    )
    monkeypatch.setattr(release_preflight, "_repository_head", lambda: "0013_expected")

    async def database_probe(_url: str) -> list[str]:
        if isinstance(revisions, Exception):
            raise revisions
        return revisions

    async def redis_probe() -> None:
        if redis_error is not None:
            raise redis_error

    monkeypatch.setattr(release_preflight, "_database_revisions", database_probe)
    monkeypatch.setattr(release_preflight, "verify_redis_connection", redis_probe)


@pytest.mark.asyncio
async def test_matching_migration_and_redis_pass_without_certifying_workers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, revisions=["0013_expected"])

    assert await release_preflight.run_check() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["checks"]["migrations"] == {
        "status": "ok", "repository_head": "0013_expected", "database_revisions": ["0013_expected"],
    }
    assert report["checks"]["redis"] == {"status": "ok"}
    assert report["not_verified"] == ["celery_worker", "celery_beat", "payment_providers"]
    assert "secret" not in json.dumps(report)


@pytest.mark.asyncio
async def test_missing_or_outdated_migration_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, revisions=[])

    assert await release_preflight.run_check() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["migrations"]["status"] == "mismatch"
    assert report["checks"]["migrations"]["database_revisions"] == []
    assert report["checks"]["redis"]["status"] == "ok"


@pytest.mark.asyncio
async def test_database_error_is_redacted_and_redis_still_checked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, revisions=RuntimeError("password=very-secret postgres://private"))

    assert await release_preflight.run_check() == 3
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["checks"]["migrations"] == {"status": "error", "error_type": "RuntimeError"}
    assert report["checks"]["redis"] == {"status": "ok"}
    assert "very-secret" not in output
    assert "private" not in output


@pytest.mark.asyncio
async def test_redis_error_does_not_hide_migration_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(
        monkeypatch, revisions=["0012_old"],
        redis_error=ConnectionError("rediss://user:password@private-host"),
    )

    assert await release_preflight.run_check() == 3
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["checks"]["migrations"]["status"] == "mismatch"
    assert report["checks"]["redis"] == {"status": "error", "error_type": "ConnectionError"}
    assert "password" not in output
    assert "private-host" not in output


@pytest.mark.asyncio
async def test_wrong_database_scheme_does_not_probe_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(monkeypatch, revisions=["0013_expected"])
    monkeypatch.setattr(
        release_preflight,
        "get_settings",
        lambda: SimpleNamespace(database_url="sqlite+aiosqlite:///:memory:", redis_url="redis://host/0"),
    )

    async def forbidden_probe(_url: str) -> list[str]:
        raise AssertionError("database probe must not run")

    monkeypatch.setattr(release_preflight, "_database_revisions", forbidden_probe)
    assert await release_preflight.run_check() == 3
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["migrations"] == {"status": "error", "error_type": "ValueError"}
    assert report["checks"]["redis"] == {"status": "ok"}


def test_repository_head_resolves_outside_project_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert release_preflight._repository_head() == "0013_phase7_marketing_rewards"


@pytest.mark.asyncio
async def test_database_probe_only_executes_read_query_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []
    disposed = False

    class Result:
        def scalars(self):
            return self

        def all(self):
            return ["0013_expected"]

    class Connection:
        async def execute(self, statement):
            executed.append(str(statement))
            return Result()

    class Context:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class Engine:
        def connect(self):
            return Context()

        async def dispose(self):
            nonlocal disposed
            disposed = True

    monkeypatch.setattr(release_preflight, "create_async_engine", lambda *_args, **_kwargs: Engine())
    assert await release_preflight._database_revisions("postgresql+asyncpg://ignored") == [
        "0013_expected"
    ]
    assert executed == ["SELECT version_num FROM alembic_version"]
    assert disposed
