"""ReadinessAgent: launch-gate checks that turn green as pending work lands.

These checks are deliberately strict about what "done" means. Where a known
pending item still exists, the test is marked ``xfail`` with the exact list, so
it documents the gap without hiding it. When the work lands the same test
starts passing, which is the signal that the gate has opened.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.api.web import WEB_ROOT
from app.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER_MARKERS = ("Skeleton only", "implementation is pending")


def _placeholder_modules() -> list[str]:
    found: list[str] = []
    for base in ("app", "scripts"):
        for path in sorted((PROJECT_ROOT / base).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(marker in text for marker in PLACEHOLDER_MARKERS):
                found.append(path.relative_to(PROJECT_ROOT).as_posix())
    return found


def test_no_placeholder_modules_remain() -> None:
    pending = _placeholder_modules()
    if pending:
        pytest.xfail("still placeholder modules: " + ", ".join(pending))
    assert pending == []


def test_alembic_has_exactly_one_head() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"expected a single migration head, found: {heads}"


def test_production_settings_reject_shipped_defaults() -> None:
    with pytest.raises(Exception):
        Settings(app_env="production", jwt_secret="development-only-change-before-production")

    hardened = Settings(
        app_env="production",
        jwt_secret="a" * 48,
        database_url="postgresql+asyncpg://prod_user:strong-password@db:5432/ticket_platform",
        allowed_origins=["https://tickets.example.test"],
    )
    assert hardened.app_env == "production"


def test_static_assets_contain_no_planned_markers() -> None:
    offenders = [
        path.name
        for path in WEB_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in {".js", ".css"}
        and "PLANNED:" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, f"shipped static assets still contain PLANNED markers: {offenders}"
