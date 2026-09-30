"""The test harness must not present local GREEN as production approval."""

from scripts.run_test_agents import AGENTS, release_gate_summary


def _result(name: str, *, skipped: int = 0, xfailed: int = 0) -> dict[str, object]:
    return {
        "agent": name, "status": "PASS",
        "counts": {"passed": 1, "skipped": skipped, "xfailed": xfailed},
    }


def test_report_exposes_unverified_infrastructure_and_test_gaps() -> None:
    selected = list(AGENTS)
    gates = {"database": None, "redis": None, "test_database": None}
    results = [_result(name) for name in selected]
    results[-1] = _result(selected[-1], skipped=9)
    results[-2] = _result(selected[-2], xfailed=1)

    summary = release_gate_summary(selected, gates, results)
    assert summary["production_ready"] is False
    reasons = summary["unverified_or_blocked"]
    assert any("PostgreSQL" in reason for reason in reasons)
    assert any("Redis" in reason for reason in reasons)
    assert any("9 test(s) were skipped" == reason for reason in reasons)
    assert any("1 expected-failure" in reason for reason in reasons)


def test_report_never_certifies_production_even_with_all_local_checks_green() -> None:
    selected = list(AGENTS)
    reachable = {"reachable": True}
    gates = {"database": reachable, "redis": reachable, "test_database": reachable}
    summary = release_gate_summary(selected, gates, [_result(name) for name in selected])
    assert summary["production_ready"] is False
    assert summary["external_approval"] == "not assessed"
    assert summary["unverified_or_blocked"] == []


def test_subset_report_cannot_claim_complete_coverage() -> None:
    selected = ["LedgerAgent"]
    reachable = {"reachable": True}
    gates = {"database": reachable, "redis": reachable, "test_database": reachable}
    summary = release_gate_summary(selected, gates, [_result("LedgerAgent")])
    assert "Only a subset of test suites was run" in summary["unverified_or_blocked"]
