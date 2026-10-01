"""Neon connection handling must preserve TLS server authentication."""

import ssl

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
