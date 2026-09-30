#!/usr/bin/env python
"""Multi-agent test harness for the e-ticket platform.

Each "agent" is one independent, domain-scoped test suite with its own
pass/fail contract. The runner executes every agent in isolation, checks the
environment gates (PostgreSQL / Redis / test database), and prints a readiness
report. This is the "many agents" entry point: when the project is ready, this
single command exercises every domain.

Usage::

    python scripts/run_test_agents.py
    python scripts/run_test_agents.py --only P2PAgent,LedgerAgent,ContractAgent
    python scripts/run_test_agents.py --json tmp/test_agents_report.json
    python scripts/run_test_agents.py --list

Environment gates:
    TICKET_DATABASE_URL       application database (PostgreSQL in production)
    TICKET_REDIS_URL          Celery broker / cache
    TICKET_TEST_DATABASE_URL  isolated PostgreSQL for the ConcurrencyAgent;
                              without it those tests report ``skipped``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASETEMP = PROJECT_ROOT / "tmp" / "pytest-basetemp"

# Agent name -> pytest targets. Ordering is human-readable, not execution-critical.
AGENTS: dict[str, list[str]] = {
    "AuthAgent": [
        "tests/api/test_auth.py",
        "tests/api/test_profile_read.py",
        "tests/unit/test_auth_service.py",
        "tests/unit/test_security.py",
    ],
    "LedgerAgent": [
        "tests/unit/test_money.py",
        "tests/unit/test_ledger_service.py",
        "tests/unit/test_wallet_service.py",
        "tests/unit/test_phase9_integrity.py",
    ],
    "OrderAgent": [
        "tests/unit/test_ticket_catalog_and_orders.py",
        "tests/api/test_phase3_catalog_orders.py",
    ],
    "P2PAgent": [
        "tests/unit/test_p2p_service.py",
        "tests/unit/test_p2p_tasks.py",
        "tests/api/test_phase4_p2p_api.py",
    ],
    "PaymentAgent": [
        "tests/unit/test_payment_orchestrator.py",
        "tests/unit/test_payment_tasks.py",
        "tests/api/test_phase5_payments_api.py",
        "tests/api/test_phase5_webhook_api.py",
    ],
    "DrawAgent": [
        "tests/unit/test_winner_service.py",
        "tests/unit/test_draw_tasks.py",
        "tests/api/test_phase6_results_api.py",
    ],
    "MarketingAgent": [
        "tests/unit/test_phase7_coupon_revenue.py",
        "tests/unit/test_phase7_marketing_rewards.py",
        "tests/api/test_phase7_marketing_revenue_api.py",
        "tests/api/test_phase7_rewards_api.py",
    ],
    "FrontendAgent": [
        "tests/api/test_phase8_web.py",
        "tests/api/test_phase8_read_api.py",
        "tests/api/test_public_v2_web.py",
        "tests/api/test_account_v2_web.py",
        "tests/api/test_commerce_v2_web.py",
        "tests/api/test_v2_route_inventory.py",
    ],
    "SecurityAgent": [
        "tests/unit/test_config.py",
        "tests/api/test_health.py",
        "tests/api/test_phase9_operations.py",
        "tests/api/test_phase9_audit_api.py",
        "tests/api/test_admin_users_read.py",
        "tests/api/test_admin_package_read.py",
        "tests/api/test_notification_read.py",
    ],
    "ContractAgent": ["tests/agents/test_agent_contract.py"],
    "ReadinessAgent": [
        "tests/agents/test_agent_readiness.py",
        "tests/agents/test_agent_runner.py",
        "tests/unit/test_maintenance_scripts.py",
    ],
    "ConcurrencyAgent": ["tests/concurrency"],
}

COUNT_PATTERN = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|warnings?)"
)


def _probe(url: str | None, default_port: int) -> dict[str, object] | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or default_port
    try:
        with socket.create_connection((host, port), timeout=1.0):
            reachable = True
    except OSError:
        reachable = False
    return {"url_scheme": parsed.scheme, "host": host, "port": port, "reachable": reachable}


def environment_gates() -> dict[str, object]:
    database_url = os.environ.get("TICKET_DATABASE_URL")
    redis_url = os.environ.get("TICKET_REDIS_URL")
    test_database_url = os.environ.get("TICKET_TEST_DATABASE_URL")
    return {
        "database": _probe(database_url, 5432),
        "redis": _probe(redis_url, 6379),
        "test_database": _probe(test_database_url, 5432),
        "concurrency_tests_enabled": bool(test_database_url),
    }


def _parse_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for number, kind in COUNT_PATTERN.findall(output):
        if kind in {"warning", "warnings"}:
            continue
        normalized = "error" if kind == "errors" else kind
        counts[normalized] = counts.get(normalized, 0) + int(number)
    return counts


def run_agent(name: str, targets: list[str], timeout: int) -> dict[str, object]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        f"--basetemp={BASETEMP}",
        *targets,
    ]
    started = time.time()
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        return_code = completed.returncode
    except subprocess.TimeoutExpired as expired:
        output = f"{expired.stdout or ''}\n{expired.stderr or ''}"
        return_code = 124
    counts = _parse_counts(output)
    failed = counts.get("failed", 0) + counts.get("error", 0)
    status = "PASS" if return_code == 0 and failed == 0 else "FAIL"
    return {
        "agent": name,
        "status": status,
        "return_code": return_code,
        "counts": counts,
        "seconds": round(time.time() - started, 1),
        "targets": targets,
    }


def _print_environment(gates: dict[str, object]) -> None:
    print("Environment gates")
    print("-" * 72)
    for label, key, default_port in (
        ("PostgreSQL (app db)", "database", 5432),
        ("Redis (broker)", "redis", 6379),
        ("PostgreSQL (test db)", "test_database", 5432),
    ):
        probe = gates.get(key)
        if probe is None:
            print(f"  {label:<22} not configured  (set the matching TICKET_* URL)")
        else:
            mark = "reachable" if probe["reachable"] else "NOT reachable"
            print(f"  {label:<22} {probe['host']}:{probe['port']}  {mark}")
    if not gates["concurrency_tests_enabled"]:
        print("  note: ConcurrencyAgent race tests will report 'skipped' without TICKET_TEST_DATABASE_URL")
    print()


def _print_report(results: list[dict[str, object]]) -> None:
    print("Agent results")
    print("-" * 72)
    header = f"{'Agent':<18}{'Status':<8}{'Pass':>6}{'Fail':>6}{'Skip':>6}{'xfail':>7}{'xpass':>7}{'Sec':>7}"
    print(header)
    print("-" * 72)
    totals = {"passed": 0, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "error": 0}
    for result in results:
        counts = result["counts"]
        for key in totals:
            totals[key] += counts.get(key, 0)
        print(
            f"{result['agent']:<18}{result['status']:<8}"
            f"{counts.get('passed', 0):>6}"
            f"{counts.get('failed', 0) + counts.get('error', 0):>6}"
            f"{counts.get('skipped', 0):>6}"
            f"{counts.get('xfailed', 0):>7}"
            f"{counts.get('xpassed', 0):>7}"
            f"{result['seconds']:>7}"
        )
    print("-" * 72)
    print(
        f"{'TOTAL':<18}{'':<8}{totals['passed']:>6}"
        f"{totals['failed'] + totals['error']:>6}{totals['skipped']:>6}"
        f"{totals['xfailed']:>7}{totals['xpassed']:>7}"
    )
    print()
    if totals["xfailed"]:
        print(f"  {totals['xfailed']} check(s) are xfail: known pending work (see ReadinessAgent output).")
    if totals["skipped"]:
        print(f"  {totals['skipped']} check(s) skipped: usually PostgreSQL-only concurrency coverage.")
    if totals["xpassed"]:
        print(f"  {totals['xpassed']} xfail(s) now pass: that pending item may be ready to un-gate.")


def release_gate_summary(
    selected: list[str], gates: dict[str, object], results: list[dict[str, object]]
) -> dict[str, object]:
    """Separate passing local suites from unverified release prerequisites."""

    reasons: list[str] = []
    if set(selected) != set(AGENTS):
        reasons.append("Only a subset of test suites was run")
    for label, key in (
        ("Application PostgreSQL", "database"),
        ("Redis", "redis"),
        ("Isolated test PostgreSQL", "test_database"),
    ):
        probe = gates.get(key)
        if not isinstance(probe, dict) or not probe.get("reachable"):
            reasons.append(f"{label} is not verified reachable")
    skipped = sum(result["counts"].get("skipped", 0) for result in results)
    xfailed = sum(result["counts"].get("xfailed", 0) for result in results)
    if skipped:
        reasons.append(f"{skipped} test(s) were skipped")
    if xfailed:
        reasons.append(f"{xfailed} expected-failure check(s) remain")
    if any(result["status"] == "FAIL" for result in results):
        reasons.append("At least one test suite failed")
    # No automated test run can certify external legal/provider decisions,
    # backup restoration, alert routing, or independent draw review.
    return {
        "production_ready": False,
        "scope": "local automated tests only",
        "unverified_or_blocked": reasons,
        "external_approval": "not assessed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the multi-agent test harness.")
    parser.add_argument("--only", help="comma-separated agent names to run")
    parser.add_argument("--json", dest="json_path", help="write a JSON report to this path")
    parser.add_argument("--list", action="store_true", help="list agents and exit")
    parser.add_argument("--timeout", type=int, default=600, help="per-agent timeout in seconds")
    arguments = parser.parse_args()

    if arguments.list:
        for name, targets in AGENTS.items():
            print(f"{name:<18} {' '.join(targets)}")
        return 0

    selected = list(AGENTS)
    if arguments.only:
        requested = [name.strip() for name in arguments.only.split(",") if name.strip()]
        unknown = [name for name in requested if name not in AGENTS]
        if unknown:
            print(f"unknown agent(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
        selected = requested

    BASETEMP.mkdir(parents=True, exist_ok=True)

    gates = environment_gates()
    _print_environment(gates)

    results: list[dict[str, object]] = []
    for name in selected:
        print(f"running {name} ...", flush=True)
        results.append(run_agent(name, AGENTS[name], arguments.timeout))

    print()
    _print_report(results)

    failed_agents = [result["agent"] for result in results if result["status"] == "FAIL"]
    verdict = "GREEN" if not failed_agents else "RED"
    print(f"Test verdict: {verdict}" + (f"  (failed: {', '.join(failed_agents)})" if failed_agents else "  (executed suites passing)"))
    release_gates = release_gate_summary(selected, gates, results)
    print("Production readiness: NOT CERTIFIED")
    for reason in release_gates["unverified_or_blocked"]:
        print(f"  - {reason}")
    print("  - External approvals, backup restore, and independent audits are not assessed")

    if arguments.json_path:
        report_path = Path(arguments.json_path)
        if not report_path.is_absolute():
            report_path = PROJECT_ROOT / report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "verdict": verdict, "environment": gates, "agents": results,
                    "release_gates": release_gates,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"JSON report: {report_path}")

    return 0 if verdict == "GREEN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
