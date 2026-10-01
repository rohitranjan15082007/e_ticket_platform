# Deployment and operations runbook

Status (observed 2026-10-01): the public Vercel Function responds with HTTP 200
at `/health` (`production`) and `/ready` (PostgreSQL and Redis `ok`). Public
catalog/results reads and representative HTML/CSS/JS routes also responded.
This proves a reachable staging API, **not** applied migration head, Celery
worker/beat operation, payment settlement, or production release approval.
The public catalog had no series or packages at this check. Follow
[`ROADMAP.md`](../ROADMAP.md) for the remaining evidence and release gates.

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

Run `python -m scripts.release_preflight` from a trusted shell configured for
the intended database and Redis service. It performs a read-only Alembic
revision query plus a Redis ping and emits a redacted JSON report: exit 0 means
both checks pass, 2 means a revision mismatch, and 3 means a configuration or
probe error. It does **not** run migrations or verify Celery worker/beat,
payment providers, backups, or financial integrity. Do not expose this command
as an unauthenticated HTTP endpoint.

## Vercel staging runtime and release configuration

The deployed project imports the `e_ticket_platform` root from GitHub. The
explicit FastAPI entrypoint in `pyproject.toml` is `app.main:app`; Vercel runs
it as a Python Function. Keep `.env` out of Git and set `TICKET_*` values in
the deployment environment. Marketplace `DATABASE_URL`/`REDIS_URL` names do
not replace this application's `TICKET_DATABASE_URL`/`TICKET_REDIS_URL`.
Use non-example JWT/DB secrets, an isolated managed PostgreSQL database, and
exact HTTPS origins. Apply Alembic migrations as a separate, reviewed step;
never perform migration on Function startup. A safe first check is the
read-only `SELECT version_num FROM alembic_version;` against the intended
staging database. Compare its result to the repository's Alembic head before
deciding whether an upgrade is needed. Never point destructive tests at it.
For any internet-reachable staging environment, set `TICKET_APP_ENV=production`
and `TICKET_DEBUG=false` so the production configuration guards reject example
secrets, credentials and insecure origins. Keep all payment-method credentials
unset and `TICKET_MANUAL_UPI_ENABLED=false` during infrastructure validation.
The Python wheel build must contain the 48 files under
`app/templates_and_static/`; source-checkout page tests alone do not verify
that deployed HTML, CSS and JavaScript are packaged.

The current Celery configuration uses Redis and a persistent worker plus beat.
Deploying the FastAPI Function alone does **not** start these processes: the
15–300 second expiry, outbox, reconciliation and draw-close schedules would not
run. Choose either a separately hosted worker/beat with managed Redis, or an
explicitly tested migration to Vercel Queues and Cron. The latter is currently
beta and Vercel Hobby Cron cannot run more often than daily, so it does not
preserve this project's short polling intervals without a different plan.
Until the queue, scheduler, database and `/ready` checks work in staging, keep
all live payment methods and withdrawal operations disabled.

### Optional cPanel worker host

The existing Vercel API and managed PostgreSQL/Redis need not be moved merely
to run Celery. A cPanel account is suitable for the worker/beat only if its
provider permits persistent user processes, supports Python 3.11+, and allows
outbound connections to the same PostgreSQL and Redis services. Terminal/SSH
access alone is not proof. Check for a monitored service facility such as
`/usr/local/cpanel/scripts/cpuser_service_manager`; do not rely on an
interactive `nohup` process or minute-level cron as production supervision.
Deploy the same release commit and `TICKET_*` configuration, with secrets
outside public web directories. Run at least one worker and exactly one beat,
then verify restart behavior plus scheduled task receipt/completion in logs.
Do not enable payments or withdrawals just because both processes start.

The ordinary cPanel Python Application Manager documents WSGI/Passenger apps;
moving this FastAPI ASGI API to cPanel also requires a supported Uvicorn/ASGI
server and reverse-proxy arrangement. PostgreSQL and Redis availability on a
shared hosting plan must be confirmed separately.

References: [cPanel user-managed services](https://docs.cpanel.net/knowledge-base/web-services/the-cpuser_service_manager-script-and-the-ubic-subsystem/),
[cPanel Python application](https://docs.cpanel.net/knowledge-base/web-services/how-to-install-a-python-wsgi-application/),
[FastAPI ASGI deployment](https://fastapi.tiangolo.com/deployment/manually/).

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
