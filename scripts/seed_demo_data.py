"""Seed one inert DRAFT series for local admin UI review.

Usage:
  set TICKET_APP_ENV=development
  set TICKET_DATABASE_URL=postgresql+asyncpg://...
  python -m scripts.seed_demo_data --admin-email admin@example.com --confirm-demo-catalog

This command never creates packages, orders, payments, withdrawals, tickets,
draws, results or wallet funds. It is not a production data loader.
"""

import argparse
import asyncio
import os
import sys

from app.config import get_settings
from app.database import SessionLocal
from app.repositories.user_repository import get_user_by_email
from app.services.demo_seed_service import DemoSeedService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed an inert development catalog draft")
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--confirm-demo-catalog", action="store_true")
    return parser


async def seed(*, admin_email: str) -> str:
    async with SessionLocal() as session:
        admin = await get_user_by_email(session, admin_email.strip().lower())
        if admin is None:
            raise ValueError("Administrator email was not found")
        result = await DemoSeedService(session).seed_draft_series(
            admin_user_id=admin.id, commit=True,
        )
    return f"Demo DRAFT series {result.series_id} {'already exists' if result.existed else 'created'}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.confirm_demo_catalog:
        print("Refusing seed without --confirm-demo-catalog", file=sys.stderr)
        return 2
    if os.environ.get("TICKET_APP_ENV", "").lower() != "development":
        print("Set TICKET_APP_ENV=development explicitly", file=sys.stderr)
        return 2
    if not os.environ.get("TICKET_DATABASE_URL"):
        print("Set TICKET_DATABASE_URL explicitly", file=sys.stderr)
        return 2
    if get_settings().app_env.lower() != "development":
        print("Resolved application environment is not development", file=sys.stderr)
        return 2
    try:
        message = asyncio.run(seed(admin_email=args.admin_email))
    except Exception as error:
        print(f"Seed failed ({type(error).__name__}); no payment data was created", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
