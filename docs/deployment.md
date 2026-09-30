# Deployment and operations runbook

Status: **development workflow documented; production rollout not authorized**.

## Local verification

1. Copy `.env.example` to `.env`; replace the example JWT secret for local use.
2. Start isolated PostgreSQL and Redis, apply `alembic upgrade head`, then run the
   API and the Celery worker/beat processes. The provided Compose file is a
   development example: it contains sample database credentials and exposed
   service ports, so do not treat it as a production manifest.
3. Run `python -m pytest -q -ra --color=no`. The nine lock/concurrency tests need
   `TICKET_TEST_DATABASE_URL` pointing to a disposable database ending in
   `_test`; a local SQLite pass does not replace this run.
4. `GET /health` shows only that the API process responds. `GET /ready` checks
   PostgreSQL and Redis and returns HTTP 503 if either probe fails. It does not
   verify migrations, Celery beat ownership, provider webhooks or payment safety.

## GitHub to Vercel staging plan (not yet deployed)

Import the `e_ticket_platform` project root from GitHub. The explicit FastAPI
entrypoint in `pyproject.toml` is `app.main:app`; Vercel runs it as a Python
Function. Keep `.env` out of Git and set `TICKET_*` values in the deployment
environment. Use non-example JWT/DB secrets, an isolated managed PostgreSQL
database, and exact HTTPS origins. Apply Alembic migrations as a separate,
reviewed step before using the API; never perform migration on Function startup.

The current Celery configuration uses Redis and a persistent worker plus beat.
Deploying the FastAPI Function alone does **not** start these processes: the
15–300 second expiry, outbox, reconciliation and draw-close schedules would not
run. Choose either a separately hosted worker/beat with managed Redis, or an
explicitly tested migration to Vercel Queues and Cron. The latter is currently
beta and Vercel Hobby Cron cannot run more often than daily, so it does not
preserve this project's short polling intervals without a different plan.
Until the queue, scheduler, database and `/ready` checks work in staging, keep
all live payment methods and withdrawal operations disabled.

References: [Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi),
[Vercel Celery](https://vercel.com/docs/frameworks/backend/celery),
[Vercel Cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing).

## Production prerequisites

Set `TICKET_APP_ENV=production` with debug disabled, a randomly generated
secret held outside source control, non-example PostgreSQL credentials and exact
HTTPS origins. The application now rejects the shipped development values at
startup. Run migrations as an explicit, reviewed release step; do not couple
schema changes to every API process restart. Keep PostgreSQL/Redis on private
networks and terminate HTTPS at a reviewed proxy. Configure HSTS there only
after TLS and domain ownership are validated.

Enable a payment adapter only after the provider-specific contract, credentials,
webhook authentication, refund/reconciliation procedure, monitoring and legal
approval exist. This repository does not dispatch Telegram invoices, register
the bot webhook, execute payouts or perform automatic refunds.

## Recovery and monitoring gates

Before launch, choose recovery point/time objectives and an encrypted PostgreSQL
backup mechanism with offsite copies and restricted access. Rehearse restoration
to an isolated database and record the result. A restore must be followed by
checks for migration version, balanced journals, wallet cache consistency,
unique settlements, order/ticket allocation and durable outbox state. Never
experiment with restore or downgrade on the live financial database.

Monitor `/ready`, API errors, task failures, queue lag, stale payment/review
cases and backup freshness. Alert thresholds and on-call ownership must be
agreed before live operation. The readiness probe is deliberately not a
substitute for these alerts or an independent financial reconciliation.

## Read-only financial integrity check

From the project root, with database access configured:

- `python -m scripts.verify_journal_balance` checks every persisted journal
  group for two-sided balanced INR postings.
- `python -m scripts.reconcile_ledger` additionally rebuilds each wallet's
  available and held balances from its postings and compares the cached values.
  `--max-findings 100` caps IDs in output, not the number of rows scanned.

Both commands output one JSON report. Exit 0 means no finding in the scanned
scope, exit 2 means at least one inconsistency, and exit 3 means the scan
failed (for example, no database). An exit 0 is not proof of provider cash
reconciliation, correct prize funding, or a successful backup. These commands
never change balances or journals. Schedule them with a read-only database
role and alert on both exit 2 and exit 3. Investigate a finding before any
manual reversal or correction; never update ledger rows directly.
