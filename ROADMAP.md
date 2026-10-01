# E-Ticket Platform: completion roadmap

Updated: 2026-10-01. This is a work plan and evidence tracker, not approval to
accept real money. `PROJECT_RULES.md`, `SPEC.md`, `PROJECT_BLUEPRINT.md`, and
`docs/security_checklist.md` remain the requirements and release gates.

## Verified starting point

- The public Vercel deployment returns HTTP 200 for `/health` (`production`)
  and `/ready` (PostgreSQL and Redis both `ok`) on 2026-10-01. These probes do
  not check migrations, scheduled jobs, payment providers, or financial safety.
- Public catalog/results API reads and the main HTML/CSS/JS routes returned
  HTTP 200 on 2026-10-01. The public catalog contained zero series and zero
  packages; no real checkout journey was exercised.
- The latest local run on the aligned release base passed 159 tests and skipped
  nine PostgreSQL-only race tests. Those nine tests require a separate disposable database whose name
  ends in `_test`; their fixture truncates tables and cycles migrations.
- The local branch was fast-forwarded from `319b9b6` to Vercel's observed base
  commit `04a7d60` after reviewing its four-file diff. The roadmap, security
  changes, CI workflow, and preflight are local worktree changes, **not yet
  deployed or verified by GitHub CI**. Unrelated local files were preserved.

## Execution order and exit evidence

### 0. Align source and retain a safe staging boundary — partially verified

- [x] Compare local checkout, GitHub `main`, and Vercel's deployed commit;
      review the diff before a fast-forward or deployment. Never overwrite
      user-owned untracked files.
- [ ] Keep live payment credentials unset and manual UPI disabled while
      infrastructure is being tested. Keep real `.env` files out of Git.
- **Exit evidence:** recorded commit SHA, environment name, and a reviewed
  release diff. A Vercel `READY` badge is not runtime evidence.

### 1. Finish code-level safety and automated verification — in progress

- [x] Add a shared, fail-closed rate limit to public login/registration
      mutations, without storing raw identity, IP, or credentials in keys.
- [x] Add a continuous-integration workflow with an isolated PostgreSQL
      `_test` service. Its **first GitHub run remains pending**; never use
      staging or production databases for these destructive tests.
- [x] Add a read-only release preflight that reports the Alembic revision and
      dependency checks separately from `/ready`; do not run migrations on
      application startup or expose secrets in an unauthenticated endpoint.
      It has only been unit-tested; the staging run remains pending.
- [ ] Run the full suite and focused security tests against the exact release
      commit in CI with no skipped PostgreSQL tests; perform real browser
      journeys on desktop and mobile. The updated local worktree run passed
      159 tests with nine expected PostgreSQL-only skips.
- **Exit evidence:** test run with zero failures and zero unexplained skips;
  reviewed CI logs and browser results.

### 2. Prove staging database integrity — awaiting database access

- [ ] Read `alembic_version` on the **staging** database and compare with the
      repository head (`0013_phase7_marketing_rewards` in the current checkout).
- [ ] If behind, review a backup and run only the necessary Alembic upgrade as
      a separate release step. Do not upgrade or downgrade an unknown database.
- [ ] Run the read-only journal-balance and wallet-reconciliation commands;
      investigate every finding before any correction.
- [ ] Create a separate empty `_test` PostgreSQL database or branch and run
      the nine destructive concurrency tests there, never on staging/live.
- **Exit evidence:** revision, integrity reports, 9/9 race-test results, and
  backup/restore evidence; `/ready` alone is insufficient.

### 3. Run and monitor background jobs — awaiting cPanel capability check

- [ ] Confirm the cPanel account has Python 3.11+, outbound access to the
      existing PostgreSQL/Redis services, and permission for supervised,
      persistent processes. Terminal/SSH alone does not prove this.
- [ ] Deploy **one** Celery beat and at least one worker from the same release
      commit with the same `TICKET_*` configuration as the API. Keep secrets
      outside public web directories and source control.
- [ ] Verify worker connection, beat ownership, task receipt/completion over
      the configured 15–300-second intervals, restart behavior, and alerts.
      Do not replace these intervals with a once-per-day cron job.
- **Exit evidence:** process-manager status, redacted logs for scheduled jobs,
  restart test, and an operator/alert owner.

### 4. Complete product journeys without invented financial data — pending

- [ ] Bootstrap the first admin through the audited maintenance command, then
      configure approved series, packages, prize funding, and draw settings.
      Do not seed financial records or publish made-up product terms.
- [ ] Exercise authenticated registration, catalog, order, wallet, result,
      withdrawal-review, and admin journeys in isolated staging accounts.
- [ ] Replace placeholder Terms, Privacy, Refund, and Responsible-use pages
      with owner-approved text; review accessibility and mobile behavior.
- **Exit evidence:** recorded end-to-end outcomes and owner-approved catalog,
  information pages, and administrative procedures.

### 5. Integrate real payment and payout operations — external decisions needed

- [ ] Select approved provider(s), contracts, credentials, webhook ownership,
      receiver checks, refund funding/operator, payout executor, reconciliation
      source, and retention policy. Do not infer settlement from screenshots,
      UTRs, or receiver confirmation.
- [ ] Complete and test the currently unimplemented provider-dependent paths:
      white-label checkout initiation, Telegram invoice delivery/webhook
      registration, external notification delivery, verified withdrawal
      destination enrollment, and funded payout/refund operations as selected.
- [ ] Test signatures, duplicates, timeouts, disputes, reversals, and provider
      reconciliation in provider sandbox before enabling any method.
- **Exit evidence:** provider-specific certification, signed/deduplicated
  webhook tests, balanced journals, exact-once settlement, and an operator
  runbook. Code alone cannot supply contracts or funding authority.

### 6. Production release gate — blocked until every preceding gate passes

- [ ] Complete independent security and draw/fairness review, applicable
      legal/compliance approval, PostgreSQL backup/restore drill, monitoring,
      alerting, and incident ownership from `docs/security_checklist.md`.
- [ ] Re-run migrations, integrity checks, full CI, browser tests, worker/beat
      checks, and payment-provider sandbox scenarios on the release candidate.
- [ ] Enable only the certified payment method(s) in a controlled release;
      observe live metrics and retain a rollback/recovery plan.
- **Complete means:** the project-wide Definition of Done in
  `PROJECT_BLUEPRINT.md` is met with recorded evidence. A healthy homepage or
  `/ready` response alone never marks this roadmap complete.

## Inputs needed to unblock the next steps

1. Read-only access to the Neon staging database (or the revision from
   `SELECT version_num FROM alembic_version;`). Never send a database URL or
   password in chat.
2. cPanel Terminal results for `python3 --version`, `id -u`, and whether
   `/usr/local/cpanel/scripts/cpuser_service_manager` is available. Also
   confirm whether the plan permits always-running user processes.
3. An isolated disposable PostgreSQL `_test` database for the nine race tests.
4. Owner/provider decisions for product terms and any real-money operations;
   these cannot be invented by an implementation agent.
