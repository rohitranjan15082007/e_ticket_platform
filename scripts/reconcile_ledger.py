"""Read-only integrity report. Run as: python -m scripts.reconcile_ledger."""

import argparse
import asyncio
import json
import sys

from app.database import SessionLocal
from app.services.integrity_service import IntegrityReport, IntegrityService


def report_payload(report: IntegrityReport) -> dict[str, object]:
    return {
        "status": "ok" if report.healthy else "finding",
        "journal_groups_checked": report.journal_groups_checked,
        "unbalanced_group_count": report.unbalanced_group_count,
        "unbalanced_group_ids": [str(item) for item in report.unbalanced_group_ids],
        "wallets_checked": report.wallets_checked,
        "wallet_mismatch_count": report.wallet_mismatch_count,
        "wallet_mismatch_ids": [str(item) for item in report.wallet_mismatch_ids],
        "findings_truncated": report.findings_truncated,
    }


async def run_check(*, include_wallets: bool = True, max_findings: int = 100) -> int:
    try:
        async with SessionLocal() as session:
            report = await IntegrityService(session).scan(
                include_wallets=include_wallets, max_findings=max_findings,
            )
    except Exception as error:
        print(
            json.dumps({"status": "error", "error_type": type(error).__name__}),
            file=sys.stderr,
        )
        return 3
    print(json.dumps(report_payload(report), sort_keys=True))
    return 0 if report.healthy else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only journal and wallet integrity check")
    parser.add_argument("--max-findings", type=int, default=100, metavar="1..1000")
    args = parser.parse_args()
    if not 1 <= args.max_findings <= 1000:
        parser.error("--max-findings must be between 1 and 1000")
    return asyncio.run(run_check(max_findings=args.max_findings))


if __name__ == "__main__":
    raise SystemExit(main())
