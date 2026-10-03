# Implementation tracker

Statuses: Planned / In progress / Review / Done. A file is marked Done only when it contains
working code that was exercised by a relevant validation command. `FILE_MAP.json` is the complete
per-file inventory; this document records phase outcomes.

## Phase 1 - Foundation

Status: **Done** (2026-09-21)

Implemented:

- Repository setup, `.gitignore`, typed `TICKET_` environment configuration, Dockerfile and Docker Compose services for FastAPI, PostgreSQL, Redis and Celery.
- Async SQLAlchemy sessions plus an Alembic async environment and `0001_foundation_identity` migration.
- Identity models for users, roles and registration idempotency records.
- Password hashing, signed JWT access tokens, persisted-role RBAC and domain error handling.
- Idempotent user registration, login and `/api/v1/auth/me`, with health endpoint at `/health`.
- Celery application configuration only; no financial or live-payment task has been activated.

Validation evidence:

| Check | Result |
|---|---|
| `python -m compileall -q app alembic tests` | Passed |
| `pytest` | Passed: 7 tests |
| `alembic upgrade head` against isolated SQLite | Passed: identity/idempotency tables created at revision `0001_foundation_identity` |
| API integration test | Passed: register -> login endpoint flow |

## Phase 2 - Financial core

Status: **Done with PostgreSQL integration verification pending** (2026-09-21)

Implemented:

- Integer-paise validation that rejects floats, `Decimal`, booleans, strings and negative values.
- `BIGINT` wallet balances and journal posting amounts, with non-negative and positive-amount database checks.
- Per-wallet available/held accounts, immutable double-entry journal groups and a balanced-journal validator.
- Wallet provision, traceable test-source credit, hold and safe-release services. Every mutation uses an idempotency key, emits an audit event and locks the wallet row with `SELECT FOR UPDATE`.
- Generic canonical idempotency fingerprints that reject fractional values before a financial mutation is written.
- Alembic migration `0002_financial_core` for wallets, ledger accounts, journal groups/postings and audit logs.

Validation evidence:

| Check | Result |
|---|---|
| Phase 2 focused suite | Passed locally; PostgreSQL-only concurrency/trigger checks require an isolated test database |
| Wallet service tests | Passed: balance movement, exact replay, conflict and insufficient-funds rollback |
| Journal tests | Passed: unbalanced and cross-currency groups rejected |
| `alembic upgrade head` against isolated SQLite | Passed through financial-integrity revision `0003_financial_integrity` |

The `asyncio.gather` wallet-lock tests are included and will run when `TICKET_TEST_DATABASE_URL` points to an isolated PostgreSQL database. Docker/PostgreSQL is not installed in this workspace, so that provider-independent integration check could not run locally.

## Phase 3 - Tickets and orders

Status: **Done with PostgreSQL integration verification pending** (2026-09-21)

Implemented:

- Ticket-series catalog with INR integer-paise pricing, finite inventory counters, prize ranks, sales/draw dates and a server-owned `DRAFT -> PUBLISHED -> OPEN -> CLOSED` lifecycle.
- Multi-series ticket packages with server-side contents, optional package inventory limits and safe deactivation.
- Immutable order item/price snapshots, short ticket reservations, owner/admin order reads, cancellation and an idempotent expired-reservation cleanup task.
- Locked series inventory updates and settlement-gated allocation into unique `(series_id, serial_number)` tickets. Allocation is internal-only and refuses any order that has not already been marked `PAID` with a trusted settlement reference.
- Admin catalog APIs, public catalog APIs, authenticated order/ticket APIs, idempotency records and audit events for every Phase 3 mutation.
- Alembic revision `0004_ticket_catalog_orders` and PostgreSQL-only `asyncio.gather` race tests for oversell prevention and duplicate-serial prevention.

Validation evidence:

| Check | Result |
|---|---|
| `python -m compileall -q app alembic tests` | Passed |
| `pytest` | Phase 3 checkpoint: 37 tests passed; 4 PostgreSQL-only row-lock tests skipped without `TICKET_TEST_DATABASE_URL` |
| Ticket/order unit tests | Passed: catalog lifecycle, inventory reservation/release, expiry, package allocation and exact replay |
| Catalog/order API test | Passed: admin catalog -> buyer order; no settlement endpoint exists |
| SQLite Alembic round trip | Passed through `0004_ticket_catalog_orders`, downgrade and re-upgrade |
| PostgreSQL offline migration SQL | Generated successfully through `0004_ticket_catalog_orders` |

The real PostgreSQL concurrency tests are present but cannot run in this workspace until an isolated `postgresql+asyncpg` URL ending in `_test` is supplied. The cleanup task remains available for the deployment's chosen cadence; the later Phase 4 work adds Celery beat schedules for P2P expiry and outbox dispatch.

## Phase 4 - P2P withdrawal matching and settlement

Status: **Implementation complete locally; isolated PostgreSQL race verification and real provider/deployment configuration pending** (2026-09-23)

Implemented:

- Snapshotted integer-paise withdrawal eligibility, default 50% floor rule, disabled-by-default threshold override, exactly one unresolved request per user, and one named wallet hold per request.
- Exact FIFO order/withdrawal matching under locks, frozen controlled-verified recipient snapshots, one active match per order/request, and no split matching.
- Buyer payment claims, receiver confirmation, late/duplicate/wrong-amount review, expiry reconciliation, disputes, and evidence-required administrator settlement or unpaid closure.
- Atomic settlement from user withdrawal-held liability to `platform:p2p-order-funds-pending:inr`; the receiver wallet is never credited by the buyer's direct payment. Settlement, order status, audit records, and delivery outbox persist together.
- Signed raw-body HMAC provider callbacks at `POST /api/v1/payments/provider-webhook`, with durable external-event replay protection, immutable match/reference/amount/currency/recipient cross-checks, and review-only handling for uncertainty. The adapter is disabled without a configured secret.
- Separate delivery retry through settlement-gated allocation; failed delivery remains paid and retry does not debit again. A transactional outbox dispatcher creates durable in-app notifications and retries delivery/notification work without reversing settlement.
- Admin-only post-settlement refund-case creation with verified original receipt, funding/destination-validation/executor references, and `PENDING_EVIDENCE` state only. It performs no payout, wallet release, or `REFUNDED` transition.
- User withdrawal/P2P APIs, dispute/admin APIs, provider webhook API, Celery beat schedules for expiry/outbox dispatch, migrations `0005_p2p_matching_settlement`, `0006_p2p_refund_case`, and `0007_p2p_provider_events_outbox`, plus API, service, migration, rollback, and PostgreSQL-only race coverage.

Validation evidence:

| Check | Result |
|---|---|
| `python -m compileall -q app tests alembic` | Passed |
| `pytest -q -ra` | Passed: 63 tests; 8 PostgreSQL-only tests skipped without `TICKET_TEST_DATABASE_URL` |
| Phase 4 service/API tests | Passed: eligibility snapshots, exact matching, claims/confirmation, signed webhook authentication/replay, provider-review outcomes, one-time settlement, rollback, delivery retry, notification outbox, refund-case safety, and authorization |
| SQLite Alembic round trip | Passed through `0007_p2p_provider_events_outbox`, downgrade, and re-upgrade including the `0006 -> 0004 -> head` re-upgrade path |
| PostgreSQL offline migration SQL | Generated successfully through `0007_p2p_provider_events_outbox` |

The four P2P race tests and the earlier financial/catalog races require an isolated PostgreSQL `postgresql+asyncpg` database whose name ends in `_test`. Docker Compose now starts a Celery beat process for expiry/outbox polling. Real deployment still requires an approved provider contract/secret, a PostgreSQL test run, and decisions for external notification/payout channels.

## Phase 5 - Other payment methods

Status: **Implementation complete locally; isolated PostgreSQL race verification and external provider/bot operations remain pending** (2026-09-24)

Implemented:

- Separate order-linked payment attempts for manual UPI, Telegram Stars, and a deliberately non-initiating white-label boundary. Every attempt freezes its order INR-paise amount, provider units, expiry, merchant reference, and payment instructions.
- Manual UPI destinations require accountable administrator approval and a database-enforced single active destination. UTR/screenshot proofs are immutable evidence only; exact amount/currency still require an explicit audited administrator decision before settlement.
- Scheduler-independent late-proof policy: evidence submitted after instruction expiry is retained only in `UNDER_REVIEW`, whether the expiry worker ran before or after the proof. It cannot settle automatically.
- Signed white-label raw-body HMAC callbacks with durable event/digest replay protection, required nonblank unique provider payment references for success, exact frozen amount/currency checks, and review-only handling for uncertainty. Checkout creation remains unavailable until a concrete provider contract is selected.
- Telegram Stars signed invoice-payload validation, `XTR` provider-unit separation, configured header-secret authentication, update replay protection, unique charge IDs, pre-checkout validation/acknowledgement deadline, and settlement only after a distinct successful-payment update.
- One trusted-settlement path into a balanced external-receipt journal, one settlement record, audit history, and a generic ticket-delivery outbox. Generic reconciliation creates review snapshots only and never polls a provider/bank or auto-settles.
- Alembic migrations `0008_phase5_payment_core` and `0009_phase5_manual_destination_uniqueness`, generic expiry/outbox/reconciliation tasks, API and webhook routes, focused service/API/migration coverage, and PostgreSQL advisory locks for missing idempotency/manual-destination logical keys.

Validation evidence:

| Check | Result |
|---|---|
| `python -m compileall -q app alembic tests` | Passed |
| Focused Phase 5 service/API/task/migration suite | Passed: 15 tests; SQLite migration upgrade/downgrade/re-upgrade through `0009_phase5_manual_destination_uniqueness` |
| `alembic heads` | Passed: one head, `0009_phase5_manual_destination_uniqueness` |
| `python -m pytest -q -ra --color=no` | Passed: 78 tests; 8 PostgreSQL-only tests skipped without `TICKET_TEST_DATABASE_URL` |
| Phase 5 OpenAPI contract check | Passed: all eight Phase 5 routes are mounted exactly once |

The PostgreSQL-only concurrency coverage remains gated by `TICKET_TEST_DATABASE_URL`; Docker/PostgreSQL is unavailable in this workspace. The Telegram integration intentionally does not call `sendInvoice`, `setWebhook`, `refundStarPayment`, or `getStarTransactions`; a separately operated bot must own invoice dispatch/webhook registration, and real provider selection/reconciliation remain pre-launch work.

## Phase 6 - Winners/results

Status: **Implementation complete locally; PostgreSQL lock-path verification and external fairness/funding operations remain pending** (2026-09-24)

Implemented:

- One-draw-per-series persistence: committed seed digest, immutable allocated-ticket candidate snapshots, frozen prize snapshots, deterministic winner transcript, one prize award per winner, and durable local winner notices.
- Sales can only transition from PUBLISHED to OPEN after a commitment exists; a post-commit catalog edit is rejected. The public sellable catalog exposes the commitment and its timestamp, while the raw seed remains private until publication.
- An ordered COMMITTED -> SALES_CLOSED -> DRAWN -> PRIZES_POSTED -> RESULT_PUBLISHED service workflow. The legacy generic catalog close path now rejects bypasses; closure runs through the draw service and atomically freezes candidate/prize evidence under locks.
- Commit/reveal selection using sha256-rejection-sampling-v1: no API accepts a preferred ticket ID, a reveal must match the pre-sale SHA-256 commitment, and published serial-only evidence lets a reader recompute candidate/result digests without exposing owner identities. Candidate manifests are paged at 1,000 serials maximum, rather than being stored repeatedly in idempotency responses.
- One idempotent, balanced existing-wallet credit per prize, with immutable award-to-winner/journal/idempotency links. Publication is blocked until every award exists and creates local winner-notification rows in the same audited transaction.
- Admin draw endpoints, public commitment/catalog and published-result endpoints, 0010_phase6_draw_results plus 0011_phase6_draw_integrity_guards migrations, and a Celery task that only closes due committed sales. PostgreSQL guards lock draw transitions, append-only evidence, and same-draw child relationships. The task cannot reveal/select/payout/publish automatically.

Validation evidence:

| Check | Result |
|---|---|
| `python -m compileall -q app alembic tests` | Passed |
| Phase 6 service/API/task/migration suite | Passed: commitment-before-open, allocated-only candidates, replay-safe prize credits, publication authorization, public reproducibility, bounded candidate pagination, scheduler boundary, and SQLite migration round trip |
| python -m pytest -q -ra --color=no | Passed locally: 87 tests; 9 PostgreSQL-only tests skipped without TICKET_TEST_DATABASE_URL |
| SQLite Alembic round trip and PostgreSQL offline SQL | Passed through 0011_phase6_draw_integrity_guards, including downgrade/re-upgrade and generated trigger/FK SQL |

The current randomness design is auditable commit/reveal code with public in-sale commitment visibility, not an independently operated randomness beacon or third-party timestamp/fairness certification. Prize posting records a balanced clearing-to-winner wallet credit; Phase 7 allocates revenue to a prize-pool account, but the award debit is not yet linked to that funded bucket. Externally sourced entropy, funded prize-reserve/payout policy, external notification provider, and PostgreSQL integration run remain pre-launch work.

## Phase 7 - Marketing and admin

Status: **Implementation complete locally; isolated PostgreSQL verification and reward funding policy pending** (2026-09-25)

Implemented:

- Coupon configuration, capacity/per-user limits, order-creation reservation, frozen gross/discount/net snapshots, settlement consumption, and cancellation release.
- One balanced allocation for each trusted external or P2P settlement: 45% prize pool, 20% marketing, 10% operations, 5% emergency reserve, and 20% profit/growth. Integer remainder is assigned to profit/growth. An immutable allocation record, idempotency record, audit event, and report are written with the settlement, not during delivery retries.
- One active referral program and cashback campaign at a time, user referral profiles/claims before first settlement, settlement-qualified pending rewards, administrator-configured affiliate accounts, evidence-backed one-order conversion, and pending commission records. Self-referral and self-affiliate attribution are blocked.
- Audited, idempotent admin activation/disable/suspend and reward/conversion void actions. Existing Phase 4-5 payment, dispute, refund-case, and manual evidence review tools remain the administrator decision paths for those domains.
- Alembic revisions `0012_phase7_coupon_revenue` and `0013_phase7_marketing_rewards`, typed admin/user API routes, API/service tests, and SQLite migration downgrade/re-upgrade coverage.

Validation evidence:

| Check | Result |
|---|---|
| `python -m compileall -q app alembic tests` | Passed |
| Phase 7 marketing service/API tests | Passed: pending-only rewards, referral self/first-order guard, cashback replay, affiliate settlement/evidence/self guard, admin RBAC, review lifecycle |
| SQLite Alembic round trip and PostgreSQL offline SQL | Passed through `0013_phase7_marketing_rewards` |
| `python -m pytest -q -ra --color=no` | Passed locally: 97 tests; 9 PostgreSQL-only tests skipped without `TICKET_TEST_DATABASE_URL` |

Marketing reward records deliberately remain `PENDING_REVIEW`: no wallet credit, payout, or debit from the marketing bucket occurs until a funded source, approval/finality, reversal, and refund clawback policy is selected. Revenue allocation does not yet fund a prize payout: the Phase 6 prize-credit path still debits clearing. PostgreSQL lock/trigger execution, provider operations, and production reconciliation remain pre-launch verification work.

## Phase 8 - Frontend

Status: **Implemented locally; browser-assisted accessibility and end-to-end payment operations remain unverified** (2026-09-25)

Implemented:

- Same-origin responsive public catalog, package, result, registration and login pages; user checkout, dashboard, ticket, wallet, withdrawal and referral pages; and read-only administrator overview/queue/revenue/marketing pages.
- Three-stage checkout (product selection, server-priced order review, payment path) with owner-only checkout-state recovery from the dashboard. Manual UPI and P2P claim forms collect the amount actually paid instead of asserting the expected order amount; evidence remains under review until backend verification and settlement.
- Owner-only wallet balance/journal and withdrawal/destination reads, idempotent empty-wallet provisioning, and admin-only dashboard, series, withdrawal and dispute reads. Static page routing uses a same-origin CSP.
- Labeled controls, semantic headings, skip navigation, live status updates, visible keyboard focus and mobile CSS. Sensitive values are placed into DOM text nodes rather than raw HTML.

Validation evidence:

| Check | Result |
|---|---|
| `python -m compileall -q app tests` | Passed |
| `node --check` on frontend JS modules | Passed |
| Phase 8 API and web route tests | Passed: 5 tests including every page, static assets, CSP, owner and admin boundaries |
| `python -m pytest -q -ra --color=no` | Passed locally: 102 tests; 9 PostgreSQL-only tests skipped without `TICKET_TEST_DATABASE_URL` |

Operational boundaries: no actual file-upload UI, self-service verified-destination enrollment, Telegram invoice dispatch, provider certification, or live payout/payment activation. Administrator financial decision APIs remain available but are intentionally not exposed as one-click UI approvals. Browser-based visual/accessibility and full payment-provider end-to-end tests remain pending; Phase 8's static/API smoke tests do not claim them.

## Phase 9 - Local production-hardening implementation

Status: **Local implementation complete; verification deferred; production launch blocked** (2026-09-25). The user removed verification from this phase's implementation-completion criteria. This is a scope change, not evidence that verification or external release gates passed.

Implemented in this slice:

- Fail-closed production settings reject the shipped JWT secret, sample PostgreSQL password, debug mode and insecure/wildcard origins; production requires PostgreSQL credentials and exact HTTPS browser origins.
- Defensive response headers and no-store on HTML/API. The existing CSP remains on web pages.
- Separate liveness (`/health`) and bounded PostgreSQL/Redis readiness (`/ready`); dependency failure returns 503 without connection error text.
- Read-only journal and wallet-cache integrity scans with bounded finding IDs, JSON output and distinct non-zero finding/error exits. They never update ledger state; a production PostgreSQL scan is still pending.
- Domain-wise test report now separates local `GREEN` test results from production release gates. The report cannot certify legal/provider approval, backup restoration or an independent draw audit.
- Administrator-only audit metadata read is bounded, filterable, newest-first, and selects no state snapshots or reason text. Persisted administrator role checks and redaction/pagination boundaries have API tests.
- Concrete security launch-gate checklist and development/operations runbook. They explicitly do not represent legal approval, provider certification, backup completion or independent fairness review.

Previously recorded local validation evidence (not rerun for this scope change):

| Check | Result |
|---|---|
| Production settings, readiness and security-header tests | Passed: 3 tests |
| `python -m compileall -q app scripts tests` | Passed |
| Phase 9 integrity and release-gate tests | Passed locally; balanced/held/mismatched journals, CLI finding/error exits, and honest report gates |
| Admin audit and pagination tests | Passed locally: 8 tests covering role denial, safe projection, filters, ordering, and invalid bounds |
| Full `python -m pytest -q -ra --color=no` | Passed locally: 128 tests; 9 PostgreSQL-only skipped; 1 readiness xfail for six remaining placeholder modules |
| `python scripts/run_test_agents.py --json tmp/test_agents_report.json` | Test verdict GREEN for executed suites; release gate reports `production_ready: false` with unavailable PostgreSQL/Redis and skipped/xfail coverage |

Pre-launch gates outside the revised Phase 9 implementation scope still include independent security/compliance review, provider certification, live PostgreSQL race/load runs, backup/restore drill, alert ownership, browser accessibility/E2E verification and independent draw audit. The readiness xfail names six remaining placeholder modules: admin users, user profile, notification model, user schema, and admin/demo seed scripts. They have not been marked implemented. No production deployment or live payment method has been enabled.

## PostgreSQL integration retest - 2026-09-26

An isolated local PostgreSQL 16.15 database named `ticket_platform_test` enabled all nine previously skipped concurrency/trigger tests. The fixture now migrates to Alembic `head` and clears only mapped application rows in that disposable test database before each downgrade; it must never be used with data to keep. PostgreSQL migration issues found during the run were repaired: enum-typed account seeds, Alembic's long revision identifier, and a provider-event enum left behind by downgrade. Test fixtures were also corrected for PostgreSQL flush/rollback behavior.

| Check | Result |
|---|---|
| PostgreSQL concurrency/trigger suite | 9 passed, 0 skipped |
| Full `python -m pytest -q -ra --color=no -p no:cacheprovider --tb=short` with `TICKET_TEST_DATABASE_URL` | 137 passed, 0 skipped, 1 expected-failure readiness check, 39 warnings |
| `python scripts/run_test_agents.py --json tmp/test_agents_report.json` with `TICKET_TEST_DATABASE_URL` | 12 domain suites passed: 135 included tests passed, 0 skipped, 1 expected-failure; production readiness not certified |

This supersedes earlier notes that the nine PostgreSQL tests could not be run locally. It does not establish production readiness: the six placeholder modules, external provider/security/legal approvals, PostgreSQL load testing, Redis/worker operations, backup restore, and independent draw audit remain open.

## Payment UI preview - 2026-09-26

Added `/payment-demo` as a responsive, clearly labeled buyer-side preview of Manual UPI, P2P, Telegram Stars, and white-label provider states. Its interactive tabs change explanatory content only; they do not create orders, call payment APIs, show a payee or QR code, or claim settlement. The real authenticated checkout and admin review screens remain unchanged. This is a UI demonstration, not evidence of provider integration or real-money readiness.

## Design-first UI concept - 2026-09-26

Created `design/e-ticket-figma-board.svg`, a Figma-importable vector board for
public discovery, buyer checkout, admin payment review and mobile checkout,
with design/import notes in `design/README.md`. The board was rendered locally
and visually inspected. It is not a native `.fig` file and does not constitute
application implementation or payment verification. No production UI code was
changed in this design-first step; design review precedes implementation.

## Complete UI direction 02 - 2026-09-28

Created and locally reviewed a different, nine-screen design proposal in
`design/v2/complete-ui-board.svg`, plus `design/v2/preview.html` and a
page-by-page handoff in `design/v2/page-details.md`. A rendered review image
is stored at `design/v2/complete-ui-board-preview.png`. The design covers login,
sign up, packages, dashboard, ticket/scratch-card visual, payment selection,
withdrawals, admin overview and admin function/criteria sections. The SVG
parsed as XML and both SVG and HTML previews rendered in headless Chrome.
Illustrative balances/prices/serials are explicitly sample data; preview
buttons make no transactions. This is design work, not a frontend or payment
implementation.

A native Figma Design file was created at
`https://www.figma.com/design/LSp6uJCfitMR3Elu715Pjk`, but the attempted
canvas write was stopped by a Figma approval/usage limit before screens were
added. The file must be treated as blank; `design/v2/build_figma.js` is
prepared source only, not executed output. Import the SVG manually or resume
native Figma construction after the limit is resolved. Existing application
UI and live provider status are unchanged.

## UI page architecture before implementation - 2026-09-28

At the user's request, UI coding was paused before edits. The proposed
navigation and screen inventory is documented in `design/v2/site-architecture.md`:
22 main pages, 16 detail/access/workflow subpages and 6 information pages (44
views total). The development-only `/payment-demo` is excluded; the current
application has 19 product web routes. A grouped sitemap, route-status tables,
navigation rules and findability checks are included. These are design/planning
counts, not 44 implemented routes or approved information-page content. No
application UI code was changed in this architecture step.

## Public UI v2 first batch - 2026-09-29

Implemented the first six views from `design/v2/site-architecture.md`: Home,
Series, Packages, Results, Login and Sign-up. The warm-ivory/violet visual
system is scoped to these pages in `public-v2.css`; user/admin/payment-demo
styling is unchanged. Catalog cards and result proofs remain API-driven, and
checkout still rechecks amounts and availability. Auth forms retain the
existing endpoints, with stronger field and busy states. No additional routes
or payment settlement behavior were introduced.

Focused route/module tests and desktop/mobile browser rendering were exercised.
The full local suite finished with **131 passed, 9 PostgreSQL-only skipped,
1 expected failure**. JavaScript syntax checks passed for the changed modules.
The browser could render the static Home/Login shell, but the catalog API
returned HTTP 500 in the local session because its database was unavailable;
live product-card data and real auth/payment flows were not visually verified.
The rest of the proposed 44-view redesign remains pending.

## Account UI v2 second batch - 2026-09-29

Implemented Dashboard, Tickets, Orders and Order detail in the warm-ivory/violet
UI direction. `/orders` and `/orders/{id}` are new web views over existing
owner-scoped order APIs. Dashboard cards use server-reported order, ticket and
wallet data; absent wallets are labeled, not treated as zero. Orders support
client-side search and status filtering. Ticket styling is decorative and
cannot decide an outcome; only server-recorded winner data is displayed.
Desktop and 390px mobile layouts were reviewed with intercepted sample API
responses (no sample data was shipped to the application). Live database-backed
auth, order and ticket flows remain unverified in this local environment.
The full local suite finished with **133 passed, 9 PostgreSQL-only skipped,
1 expected failure**; changed JavaScript modules passed syntax checks.
Checkout, wallet, withdrawal, admin and remaining planned views retain their
earlier UI until later batches. No payment settlement behavior changed.

## Commerce UI v2 third batch - 2026-09-29

Implemented the Checkout, Payments, Wallet and Withdrawals views in the v2
visual direction. `/payments` is a new authenticated web view of owner-scoped
order payment states. Checkout now fetches a read-only authenticated
`/api/v1/payment-methods` availability hint before presenting methods: Manual
UPI requires configuration and an active approved destination, Telegram Stars
requires complete bot configuration, P2P may queue, and white-label initiation
remains unavailable. The server rechecks every attempted method and remains
authoritative for settlement. Submitted UTRs, claims and prepared invoices are
not shown as paid.

Wallet balances and ledger entries come from the server. If a wallet does not
exist, the UI offers creation of an **empty** wallet without suggesting that
money was added. Withdrawals show server balances and only operations-verified
destinations; eligibility is explicitly checked on submit because there is no
read-only eligibility-preview API. An unresolved request prevents another
request form, and only unmatched requests expose cancellation. Existing
payment/withdrawal mutation endpoints and backend financial rules are unchanged.

The four page shells and payment-method safety hints have focused regression
tests. The full local suite passed with **135 passed, 9 PostgreSQL-only skipped,
1 expected failure**. Desktop and 390px mobile layouts were inspected in a
headless browser using intercepted sample API responses, which are not shipped
to the application. Real database-backed checkout, provider delivery, payout
and live browser E2E flows remain unverified here. Fourteen of the proposed 44
views now have v2 styling; admin and remaining planned/information views are
still pending.

## Remaining UI route coverage - 2026-09-29

Added shared v2 shells and route-specific rendering for the remaining public,
account and admin destinations. The 44-view product route inventory in
`design/v2/site-architecture.md` is now mapped (the development-only
`/payment-demo` is excluded). New views include catalog/result details,
How it works, configuration-aware payment methods, referrals, ticket and
withdrawal details, account information, admin dashboard/navigation, catalog,
draw, payment, withdrawal, dispute, revenue, marketing and bounded audit
reads. Catalog, result and account views use existing server APIs. Admin
queues/reports remain role-gated and read-only in this UI; no payment
verification, winner selection or payout shortcut was added.

**Route coverage is not functional completion or launch readiness.** Bounded,
role-gated admin user list/detail reads and an owner-scoped delivered P2P/draw
notification list were added after the initial route pass. Admin package reads
now include inactive records. The generic profile/notification model
placeholders remain; several admin detail/editor views are read-only or
limited to records returned by existing queues. Terms, Privacy, Refund policy
and Responsible use pages
explicitly say owner-approved content has not been supplied; they are not
published policy text. External payment/provider, payout, security and
deployment gates remain unchanged.

The route-pass local suite completed with **137 passed, 9 PostgreSQL-only
skipped, 1 expected failure**; newer backend read tests are recorded below.
All new route shells and assets passed focused tests;
changed JavaScript passed syntax checks. Headless browser visual QA with
intercepted sample responses reviewed admin desktop/mobile, public
information and account mobile views. The 390px pages inspected had no
horizontal overflow; sample responses were confined to ignored `tmp/`
artifacts. Real database-backed browser journeys and provider operations
remain unverified in this environment.

### UI read-data follow-up

Added masked administrator user list/detail reads, an authenticated delivered
P2P/draw notification list without payload fields, and administrator package
reads that include inactive catalog records. Each has a focused role/bounds or
owner-scope test. No role mutation, destination self-verification, notification
payload, or package mutation control was added to the UI.
The subsequent full local suite completed with **140 passed, 9 PostgreSQL-only
skipped, 1 expected failure**. The expected failure now names four remaining
placeholders: user profile API, generic notification model, admin-creation
script and demo-seed script.

## Readiness placeholder resolution - 2026-09-30

Replaced the four remaining skeletons with working, limited-scope
implementations. `GET /api/v1/profile` exposes only the authenticated user's
safe profile fields; edits remain disabled until an audited mutation policy is
defined. `app/models/notification.py` is a payload-free read projection over
the existing durable P2P and draw notice tables, not a redundant new table or
an external messaging integration. The first-admin maintenance command
prompts for a password, requires an explicit database URL, confirmation and
idempotency key, records an audit event, and refuses a second admin. The demo
seed command requires an explicitly selected development environment and
creates only one inert DRAFT series through the guarded catalog service; it
does not create payments, orders, tickets, results or wallet funds.

Focused tests cover authentication, payload exclusion, bootstrap replay and
single-admin gating, development-only idempotent seeding, and CLI guards.
The former placeholder readiness check now **passes**. Full local suite:
**146 passed, 9 PostgreSQL-only skipped, 0 failed, 0 xfailed**. The nine
PostgreSQL-only tests were previously exercised in an isolated local test
database, but the required test URL is not configured in this run. These
changes do not resolve external provider, policy, deployment or live-flow
gates, and neither maintenance command was executed against a user database.
The updated Auth, Frontend, Security and Readiness agent suites separately
reported **48 passed, 0 failed, 0 xfailed**; their release gate still says
production readiness is not certified.

## Pre-deployment local recheck - 2026-09-30

The first full pytest invocation reached 145 passes and nine PostgreSQL-only
skips, but one migration test could not set up because the Windows user temp
folder denied access. Rerunning with a fresh project-local `--basetemp` completed:
**146 passed, nine PostgreSQL-only skipped, zero failures**. No live database,
provider, queue, or browser end-to-end check was performed. Added
`path_separator = os` to `alembic.ini` to remove its project-owned Alembic
deprecation warning; focused migration/readiness tests then passed **6/6**.
The remaining warnings originate in installed Starlette/FastAPI dependencies.
This recheck does not change the production release gate or authorize payment
activation. At the time of this recheck, project files were still untracked in
the local Git checkout and GitHub publication had not yet been attempted.

## GitHub repository split - 2026-09-30

Published the three text requirement documents plus the public `.env.example`
and `.gitignore` to the public `rohitranjan15082007/sctratch_card` repository
(commit `9daa7ad`). Published the application source, migrations, UI assets,
design handoff, tests, scripts and operations documentation to the separate
public `rohitranjan15082007/e_ticket_platform` repository (initial app commit
`f234a7b`). The latter remote contained 292 files when checked. The real
`.env`, original reference PDF/photos, local test artifacts and missing-work
report PDF were not published. A paths-only credential-pattern scan of staged
content found no recognized token or private-key markers; that is not a full
security audit. The pre-publication local suite passed 146 tests, with nine
isolated PostgreSQL-only tests skipped because no test URL was configured.
Repository publication is not a Vercel deployment or live-payment approval.

## Staging packaging check - 2026-09-30

An isolated wheel built from the published Python packaging configuration
contained zero of the 48 files under `app/templates_and_static/`, even though
source-checkout web tests passed. Added explicit setuptools package-data globs
for HTML templates, CSS, JavaScript and the static image directory. A fresh
wheel then contained all 48 assets, and focused web-route tests passed 5/5.
The wheel check validates packaging only; no Vercel runtime, managed database,
Redis connection, worker/beat schedule or live payment was exercised.

## Phase summary

| Phase | Status | Scope |
|---|---|---|
| 2 - Financial core | Done; local PostgreSQL race/trigger tests passed | Integer paise, wallets, journals, audit/idempotency expansion, concurrency tests |
| 3 - Tickets and orders | Done; local PostgreSQL inventory races passed | Series, packages, inventory, orders and settlement-gated allocation |
| 4 - P2P | Implementation complete locally; PostgreSQL races passed; real provider/deployment configuration pending | Snapshots, holds, exact matching, signed evidence/review, settlement, delivery/notification outbox and safe refund-case recording |
| 5 - Other payment methods | Implementation complete locally; PostgreSQL migration path exercised; external provider/bot operations pending | Adapter boundaries, Telegram Stars inbound validation, manual review, signed webhooks, delivery outbox and review-only reconciliation |
| 6 - Winners/results | Implementation complete locally; PostgreSQL draw guards passed; external fairness/funding operations pending | Sales closure, commit/reveal draw, idempotent prize credits, publication and local notifications |
| 7 - Marketing/admin | Implementation complete locally; PostgreSQL migration path exercised; funded reward policy pending | Coupons, pending-review referrals/cashback/affiliates, revenue reports and review tooling |
| 8 - Frontend | Implemented locally; browser accessibility/E2E validation pending | Public pages, dashboards, responsive accessible UI |
| 9 - Local production hardening | Implementation complete; verification deferred; production blocked | Configuration/headers, readiness, integrity tools, audit metadata, and release-gate documentation; external gates tracked separately |

## Intentional boundaries

- No real provider contract/credentials, externally operated Telegram bot dispatch/registration, QR/UPI payment-initiation flow, wallet-credit source adapter, payout executor, external notification channel, or production deployment has been configured. The signed provider webhook boundaries and scheduled local outbox workers are implemented but disabled/unconfigured for live operation. Phase 5 reconciliation is review-only and has no bank/provider polling or automatic settlement.
- `SPEC.md` v1.2 is the authoritative corrected behavior for Phase 4. A screenshot/UTR/receiver confirmation never constitutes verification.
- Provider selection, legal/compliance approval, KYC/AML, fees, refund funding/execution, evidence retention, reconciliation operations and real-money launch remain explicit pre-launch decisions.

## Live administrator action console - 2026-10-03

Published the guarded `/admin/actions` workspace with all 34 existing
administrator POST/PUT operations: catalog and draw lifecycle, package
management, payment/P2P review, dispute/refund cases, and marketing review.
The browser builds exact typed payloads, uses the shared authenticated
idempotent request layer, requires explicit confirmation phrases, displays the
server response safely, and keeps backend transition rules authoritative.

The clean release branch passed **159 tests**; nine isolated PostgreSQL race
tests were skipped because no local `TICKET_TEST_DATABASE_URL` was supplied.
An authenticated local browser run created a disposable draft series and
received HTTP 201. Desktop/mobile checks found no horizontal overflow or
console/page errors, and the final axe audit reported zero violations. The
temporary local QA database was removed afterward.

The first Preview exposed a provider `postgresql://` URL that SQLAlchemy tried
to load through an unavailable synchronous driver. Runtime URL normalization
now selects `postgresql+asyncpg`, preserves verified Neon TLS, and is covered
by configuration, connection, and migration tests. The replacement Preview
served `/health`, the admin shell and all 34 deployed action definitions; its
database check passed while Preview Redis remained unconfigured. Production
deployment `dpl_2eJPdNyUCoJFMfKUSbjvKVb5kVBU` became READY from GitHub `main`
commit `33fdd65`. Live `/health` reports `production`, and `/ready` reports both
database and Redis `ok`.

Production authenticated-admin behavior still requires an existing first
administrator credential. The repository intentionally permits that initial
account only through `python -m scripts.create_admin`, which prompts for the
password and refuses an existing email or second administrator. No password,
database URL, JWT secret, payment provider, live-money settlement, or external
Celery worker/beat claim was added by this release.
