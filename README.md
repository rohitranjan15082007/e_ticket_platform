# E-Ticket Platform

FastAPI platform for limited ticket sales, wallet accounting and a controlled P2P withdrawal-matching core. It is intentionally **not configured for live payments**.

## Current status

Phases 1-3 are implemented locally: admin-managed finite ticket series/packages, server-priced orders with short inventory reservations, safe cancellation/expiry release, and settlement-gated ticket allocation with unique per-series serials.

Phase 4's P2P core is also implemented locally. It supports integer-paise withdrawal eligibility snapshots and named holds, exact FIFO matching, immutable verified-recipient snapshots, buyer payment claims, receiver confirmation, signed provider-event ingestion, evidence-required admin resolution, and one-time wallet-held to order-pending ledger settlement. Delivery and in-app notices are dispatched separately from a durable outbox.

Phase 5's non-P2P payment core is implemented locally as separate, order-linked payment attempts. Manual UPI instructions are frozen from an administrator-approved destination; a UTR or screenshot reference only enters review until an administrator records an accountable decision. Telegram Stars uses signed invoice payloads, `XTR` provider units, authenticated updates, unique charge IDs, and settles only after a `successful_payment` update—never at pre-checkout. This service creates and validates signed local Telegram instructions and handles inbound updates; a separately operated bot must dispatch invoices and register the webhook. The white-label path is a signed HMAC callback boundary, deliberately unable to create checkout sessions until a concrete approved provider adapter is selected. Every trusted settlement creates a balanced external-receipt journal, a unique settlement record, and a durable ticket-delivery outbox event.

Phase 6's draw/result workflow is implemented locally. An administrator commits a SHA-256 seed digest while a series is PUBLISHED; sales cannot open without that commitment, and the public catalog exposes its digest and timestamp once sales are open. After the sale end, the system freezes only allocated ticket serials and the configured prize ranks. Revealing the matching seed selects unique winners with documented SHA-256 rejection sampling, then posts one idempotent, balanced wallet credit per prize before an administrator can publish the result. Published results expose the seed, digest, algorithm transcript and result proof; the potentially large serial-only candidate manifest is retrieved in bounded pages at GET /api/v1/results/{series_id}/candidates. Winner owner identities and journal identifiers remain private. Publication also creates durable in-app winner notices; no external email, SMS, or push provider is configured.

Phase 7's marketing and revenue core is implemented locally. Orders freeze gross price, coupon discount, and payable net price before payment; cancellation releases an unused coupon reservation. Each trusted P2P or non-P2P settlement posts one balanced integer-paise 45/20/10/5/20 revenue allocation in the same transaction, with any rounding remainder assigned to profit/growth. Admins can configure referral and cashback campaigns and affiliate policies, review pending rewards or commissions, and void or disable them with audit reasons. Referral and cashback eligibility is checked at settlement; affiliate conversion requires an administrator's evidence reference. These marketing reward records remain pending review and do not credit wallets.

Phase 8's same-origin web frontend is implemented locally at `/`, `/series`, `/packages`, `/results`, `/login`, `/register`, the user pages (`/checkout`, `/dashboard`, `/wallet`, `/withdrawals`, `/tickets`, `/referrals`), and administrator pages under `/admin`. Checkout shows the server-priced reservation before selecting P2P, manual UPI, or configured Telegram Stars. Actual amount paid is entered separately from the expected order amount for evidence submission. A proof/claim is never rendered as settlement. Admin queue pages are read-only: payment approval and dispute resolution still require independent evidence through the controlled API/workflow. Wallet provisioning creates an empty wallet; withdrawals require an already verified destination. The frontend uses semantic HTML, labeled forms, keyboard-visible focus, live status messages, responsive CSS, and a same-origin content security policy. Tokens are kept in session storage and cleared on sign-out; production auth/session review and browser-assisted accessibility testing remain pre-launch work.

The P2P user flow is explicit:

1. A receiver selects a payment destination already verified by a controlled workflow and creates a withdrawal request.
2. A buyer creates a normal order, then calls `POST /api/v1/orders/{id}/p2p/match` to enter the exact-match queue.
3. The buyer may submit a UTR/proof and the receiver may confirm receipt; neither action settles money by itself.
4. A configured signed provider callback can verify an exact submitted reference; receiver confirmation and verified evidence are both required for normal settlement. An evidenced admin override remains available for reconciliation. The order becomes `PAID` and delivery is pending.
5. The scheduled P2P outbox worker allocates delivery and stores durable in-app notices. An administrator can retry failed delivery without a second debit. A post-settlement refund route only creates an evidence-pending case; it never performs a payout.

## Safety boundaries

- A screenshot, UTR, or receiver confirmation is never payment verification.
- There is no user endpoint to mark a payment destination verified.
- Exposed/expired P2P matches never auto-release or auto-rematch a hold.
- The generic HMAC provider-callback adapter is disabled until a selected provider's documented signing contract and `TICKET_P2P_PROVIDER_WEBHOOK_SECRET` are configured. It is not a payout executor or a claim of live-provider certification.
- Manual UPI proof is evidence only. It can settle an order only after an authenticated administrator approves the exact frozen amount and currency; expiry, a screenshot, and a UTR never auto-settle it.
- Telegram Stars is disabled by default. It needs the bot token, bot username, validated HTTPS webhook URL, webhook secret, invoice-payload secret, and explicit Bot API-call opt-in. Pre-checkout is acknowledged only after validation and within a bounded route deadline; only an authenticated successful-payment update can settle an order. This code does not call `sendInvoice`, `setWebhook`, `refundStarPayment`, or `getStarTransactions`; invoice dispatch, webhook registration, refunds, and transaction polling remain external operational responsibilities.
- The Phase 5 white-label adapter intentionally returns `UNSUPPORTED` for checkout creation until a specific provider's creation/status/refund contract is implemented. Configuring an HMAC secret alone never fabricates a payment URL or a payment result.
- Generic payment expiry moves exposed orders into reconciliation review; late manual evidence is retained only for an explicit administrator decision. The generic payment outbox retries local ticket delivery without creating another settlement.
- The P2P outbox worker/beat schedule persists local delivery and in-app notifications. The Phase 5 generic payment outbox only dispatches ticket allocation. No external email, SMS, push, or payout provider is configured.
- Provider selection, KYC/AML, legal/compliance approval, fees, refund funding, evidence retention, and production reconciliation remain pre-launch decisions.
- A draw cannot accept administrator-selected ticket IDs. The commitment must be recorded before sales open, is visible in the public catalog during sales, and locks later catalog edits; closure then requires the end of the configured sale window and zero active reservations, and audits candidate/prize snapshots before selection. PostgreSQL also enforces append-only candidate/winner/award/notification evidence and the ordered draw lifecycle.
- Draw prize posting uses the existing administrator-controlled wallet-credit ledger path (debit clearing, credit winner available balance) and is idempotent per winner. Phase 7 now allocates settled order value into revenue buckets, but prize posting is not linked to a funded prize-pool debit or external payout; those operating decisions remain pre-launch work.
- Referral, cashback, and affiliate rewards are pending-review records only. A funded marketing-account debit, payout/credit approval policy, clawback policy, and wallet credit have not been authorized or implemented.
- The scheduled draw worker only closes due, already committed series. It never reveals a seed, selects winners, posts prizes, or publishes a result. The code stores local winner notices but has no external notification delivery channel. Commit/reveal remains locally auditable rather than an independently operated randomness beacon, so externally sourced entropy and independent timestamping/certification remain pre-launch work.

## Local setup

1. Copy `.env.example` to `.env` and replace `TICKET_JWT_SECRET`.
2. Install development dependencies: `python -m pip install -e ".[dev]"`.
3. Run database migrations: `alembic upgrade head`.
4. Start the API: `uvicorn app.main:app --reload`.
5. Run tests: `pytest`.

Then open `http://127.0.0.1:8000/`. The web pages require the same database and configured backend integrations as the API. This local UI does not activate live payments or dispatch Telegram invoices.

To create the **first** administrator after migrations, explicitly set
`TICKET_DATABASE_URL` to the intended database and run
`python -m scripts.create_admin --email admin@example.com --idempotency-key first-admin-setup-1 --confirm-first-admin`.
The command prompts twice for a 12+ character password, audits creation,
replays the same key safely, and refuses an existing account or a second
administrator. Do not put the password on the command line or in source control.

For an inert local catalog example, explicitly set
`TICKET_APP_ENV=development` and `TICKET_DATABASE_URL`, then run
`python -m scripts.seed_demo_data --admin-email admin@example.com --confirm-demo-catalog`.
It creates one idempotent **DRAFT** series visible in the admin catalog only;
it never seeds funds, orders, payments, tickets, draws or results. Neither
maintenance command runs automatically.

For PostgreSQL row-lock integration tests, set `TICKET_TEST_DATABASE_URL` to a disposable, isolated `postgresql+asyncpg` database whose name ends in `_test`; those tests deliberately skip otherwise. The fixture clears application rows and runs Alembic downgrade/upgrade around each test, so never point it at a database containing data to keep.

Phase 9 local hardening implementation is complete. The nine isolated PostgreSQL
race/trigger tests have since passed locally, but the platform is not
production-approved. Security, legal, provider, PostgreSQL load, backup,
monitoring, accessibility and fairness requirements remain pre-launch gates in
`docs/security_checklist.md`.
`GET /health` is process liveness; `GET /ready` returns 503 when PostgreSQL
or Redis is unavailable. Production settings reject the example JWT/DB
credentials, debug mode and insecure browser origins. See
`docs/security_checklist.md` and `docs/deployment.md` for unresolved
external approval, provider, backup, monitoring and fairness gates.

For a read-only accounting check, run `python -m scripts.reconcile_ledger`
(journal groups plus wallet caches) or `python -m scripts.verify_journal_balance`
(journals only). Exit 2 means findings and exit 3 means the check failed. The
domain test harness can write `tmp/test_agents_report.json`; its `GREEN`
means local suites passed, not that production launch is approved.

Read `START_HERE.md`, `PROJECT_RULES.md` and `SPEC.md` before changing financial or payment behavior.
