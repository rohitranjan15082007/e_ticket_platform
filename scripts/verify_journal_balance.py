"""Read-only journal-only check. Run as: python -m scripts.verify_journal_balance."""

import asyncio

from scripts.reconcile_ledger import run_check


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_check(include_wallets=False)))
