# Multi-agent test harness

Independent, domain-scoped test suites ("agents"). Each agent has its own
pass/fail contract, so a failure points at one domain instead of a wall of
tests. The runner executes every agent and prints a test report. Its `GREEN`
verdict means the executed suites passed; it does **not** mean production approval.

## Run

```bash
# every agent
python scripts/run_test_agents.py

# a subset
python scripts/run_test_agents.py --only P2PAgent,LedgerAgent,ContractAgent

# list agents
python scripts/run_test_agents.py --list

# machine-readable report
python scripts/run_test_agents.py --json tmp/test_agents_report.json
```

The runner uses `-p no:cacheprovider` and a project-local `--basetemp`, so it
does not trip over read-only caches on synced (for example OneDrive) folders.

## Agents

| Agent | Covers |
|---|---|
| AuthAgent | register/login/me, password hashing, token validation |
| LedgerAgent | integer paise, balanced journals, idempotency, wallet holds, integrity checks |
| OrderAgent | catalog lifecycle, server pricing, reservations, expiry, cancellation |
| P2PAgent | eligibility/50% cap, hold, exact FIFO matching, claims, confirmation, settlement, timeouts |
| PaymentAgent | manual UPI review, Telegram Stars updates, white-label signed callbacks, replay |
| DrawAgent | commit/reveal, candidate freeze, winner selection, prize idempotency, publication |
| MarketingAgent | coupons, 45/20/10/5/20 revenue allocation, referral/cashback/affiliate guards |
| FrontendAgent | served pages, static assets, CSP, owner/admin read boundaries |
| SecurityAgent | fail-closed production settings, readiness probe, security headers |
| ContractAgent | the frontend's `/api/v1` calls and page wiring match the backend |
| ReadinessAgent | placeholder modules, single migration head, hardened settings, no PLANNED assets |
| ConcurrencyAgent | PostgreSQL-only row-lock/trigger/race tests (needs `TICKET_TEST_DATABASE_URL`) |

## Interpreting results

- **PASS** — the domain is green.
- **Skip** — usually the PostgreSQL-only concurrency tests. Set
  `TICKET_TEST_DATABASE_URL` to an isolated database whose name ends in `_test`
  to actually run them.
- **xfail** — a known pending item, printed with its exact reason. When the
  work lands the same test starts passing (xpass), which signals the gate has
  opened.

## Environment gates

The runner probes the configured `TICKET_DATABASE_URL`, `TICKET_REDIS_URL` and
`TICKET_TEST_DATABASE_URL`. Without PostgreSQL and Redis the run is a functional
check only — it is **not** production concurrency proof.

The JSON report includes `release_gates.production_ready: false` and lists
unverified infrastructure, skips and expected failures. Even a fully green
automated run cannot assess legal/provider approvals, backup restoration,
alert routing or the independent draw audit.
