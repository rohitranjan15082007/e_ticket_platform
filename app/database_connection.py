"""Connection options for the application's async PostgreSQL driver."""

import ssl

from sqlalchemy.engine import URL, make_url


def async_database_options(raw_url: str) -> tuple[URL, dict[str, ssl.SSLContext]]:
    """Use system CAs and hostname verification for Neon connections.

    Neon's libpq URL parameters are not all understood by asyncpg. In
    particular, asyncpg treats ``channel_binding`` as a server setting and
    searches for ``~/.postgresql/root.crt`` with ``sslmode=verify-full``.
    Supplying a verified SSL context avoids that runtime-specific file path.
    """

    url = make_url(raw_url)
    is_neon = (url.host or "").lower().endswith(".neon.tech")
    if url.drivername != "postgresql+asyncpg" or not is_neon:
        return url, {}

    query = {
        key: value
        for key, value in url.query.items()
        if key not in {"sslmode", "sslrootcert", "channel_binding"}
    }
    return url.set(query=query), {"ssl": ssl.create_default_context()}
