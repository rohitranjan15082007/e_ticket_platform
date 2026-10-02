"""Neon connection handling must preserve TLS server authentication."""

import ssl

import pytest

from app.database_connection import async_database_options


def test_neon_url_uses_verified_system_ca_context() -> None:
    url, connect_args = async_database_options(
        "postgresql+asyncpg://owner:secret@ep-example-pooler.us-east-1.aws.neon.tech/db"
        "?sslmode=verify-full&channel_binding=require&sslrootcert=/missing/root.crt"
    )

    assert url.query == {}
    assert url.password == "secret"
    context = connect_args["ssl"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_other_postgres_urls_remain_unchanged() -> None:
    raw_url = "postgresql+asyncpg://user:secret@localhost/db?sslmode=disable"
    url, connect_args = async_database_options(raw_url)

    assert url.render_as_string(hide_password=False) == raw_url
    assert connect_args == {}


def test_libpq_neon_url_is_normalized_and_uses_verified_tls() -> None:
    url, connect_args = async_database_options(
        "postgresql://owner:secret@ep-example-pooler.us-east-1.aws.neon.tech/db"
        "?sslmode=require&channel_binding=require&application_name=ticket-platform"
    )

    assert url.drivername == "postgresql+asyncpg"
    assert url.query == {"application_name": "ticket-platform"}
    assert url.password == "secret"
    context = connect_args["ssl"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_libpq_non_neon_url_preserves_query() -> None:
    url, connect_args = async_database_options(
        "postgresql://user:secret@db.example.test/ticket?application_name=ticket-platform"
    )

    assert url.drivername == "postgresql+asyncpg"
    assert url.query == {"application_name": "ticket-platform"}
    assert connect_args == {}


def test_sqlite_async_url_remains_available_for_local_migrations() -> None:
    raw_url = "sqlite+aiosqlite:///:memory:"

    url, connect_args = async_database_options(raw_url)

    assert url.render_as_string(hide_password=False) == raw_url
    assert connect_args == {}


def test_unsupported_driver_rejected() -> None:
    with pytest.raises(ValueError, match="PostgreSQL with the asyncpg driver"):
        async_database_options("postgresql+psycopg://user:secret@db/ticket")
