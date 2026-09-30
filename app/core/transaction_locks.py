"""Small cross-process locks for keys that do not yet have a database row.

``SELECT ... FOR UPDATE`` cannot lock a missing payment-reference row.  On
PostgreSQL we therefore take a transaction-scoped advisory lock derived from a
stable digest before looking up or creating that reference.  SQLite is used
only for local/unit coverage and serializes writers itself, so it intentionally
uses the same service flow without emitting a PostgreSQL-only statement.
"""

from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def advisory_lock_key(*parts: str) -> int:
    """Return a deterministic signed bigint accepted by PostgreSQL advisory locks."""

    if not parts or any(not isinstance(part, str) or not part for part in parts):
        raise ValueError("advisory lock parts must be non-empty strings")
    digest = sha256("\x1f".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def lock_key_for_transaction(session: AsyncSession, *parts: str) -> None:
    """Serialize PostgreSQL transactions that act on the same logical key.

    The lock is held until the surrounding transaction commits or rolls back;
    callers must not perform external I/O while holding it.
    """

    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": advisory_lock_key(*parts)},
    )
