"""Connection options for the application's async PostgreSQL driver."""

import ssl

from sqlalchemy.engine import URL, make_url


def async_database_options(raw_url: str) -> tuple[URL, dict[str, ssl.SSLContext]]:
    """Normalize provider PostgreSQL URLs and verify Neon TLS peer identity.

    Neon's libpq URL parameters are not all understood by asyncpg. In
    particular, asyncpg treats ``channel_binding`` as a server setting and
    searches for ``~/.postgresql/root.crt`` with ``sslmode=verify-full``.
    Supplying a verified SSL context avoids that runtime-specific file path.
    """

    url = make_url(raw_url)
    if url.drivername == "sqlite+aiosqlite":
        # Keep the local and migration-test database path available.
        return url, {}
    if url.drivername == "postgresql":
        # Provider dashboards commonly export a libpq URL. The application
        # and Alembic both use SQLAlchemy's async engine.
        url = url.set(drivername="postgresql+asyncpg")
    elif url.drivername != "postgresql+asyncpg":
        raise ValueError("DATABASE_URL must use PostgreSQL with the asyncpg driver")

    is_neon = (url.host or "").lower().endswith(".neon.tech")
    if not is_neon:
        return url, {}

    query = {
        key: value
        for key, value in url.query.items()
        if key not in {"sslmode", "sslrootcert", "channel_binding"}
    }
    return url.set(query=query), {"ssl": ssl.create_default_context()}
