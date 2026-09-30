"""Explicit one-time administrator bootstrap.

Usage:
  set TICKET_DATABASE_URL=postgresql+asyncpg://...
  python -m scripts.create_admin --email admin@example.com \
      --idempotency-key first-admin-setup-1 --confirm-first-admin

The password is prompted, never accepted on the command line. This command
refuses to elevate an existing account or create a second administrator.
"""

import argparse
import asyncio
import getpass
import os
import sys

from app.database import SessionLocal
from app.services.admin_bootstrap_service import AdminBootstrapService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create the first admin only")
    parser.add_argument("--email", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--confirm-first-admin", action="store_true")
    return parser


async def bootstrap(*, email: str, password: str, idempotency_key: str) -> str:
    async with SessionLocal() as session:
        result = await AdminBootstrapService(session).create_first_admin(
            email=email, password=password, idempotency_key=idempotency_key,
            commit=True,
        )
    return f"Admin {result.user_id} {'already existed from this key' if result.replayed else 'created'}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.confirm_first_admin:
        print("Refusing bootstrap without --confirm-first-admin", file=sys.stderr)
        return 2
    if not os.environ.get("TICKET_DATABASE_URL"):
        print("Set TICKET_DATABASE_URL explicitly before bootstrap", file=sys.stderr)
        return 2
    password = getpass.getpass("New admin password (12+ characters): ")
    if password != getpass.getpass("Confirm password: "):
        print("Passwords did not match", file=sys.stderr)
        return 2
    try:
        message = asyncio.run(bootstrap(
            email=args.email, password=password, idempotency_key=args.idempotency_key,
        ))
    except Exception as error:
        # Validation errors may echo input values; never print a prompted password.
        print(f"Bootstrap failed ({type(error).__name__}); no password was printed", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
