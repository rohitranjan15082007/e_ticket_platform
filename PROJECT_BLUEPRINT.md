# E-Ticket / Scratch Card Platform - Full Project Structure

**Version:** 1.0  
**Purpose:** Codex-ready project blueprint based on the complete 11-page handwritten requirement and the existing P2P Payment Specification v1.2.

> This document defines structure and implementation order only. It is not legal or regulatory approval. Before launch, ticket/raffle legality, KYC/AML, tax, wallet treatment, payment-provider rules, Telegram rules, refunds and applicable RBI/payment regulations must be reviewed by qualified professionals.

---

## 1. Mandatory Technology Stack

- Python 3.11+
- FastAPI
- PostgreSQL
- SQLAlchemy 2.0 async
- Alembic
- Pydantic v2
- pytest
- Celery + Redis
- Docker Compose
- Frontend: HTML, CSS and modular JavaScript

### Critical engineering rules

- Money must always be stored as integer paise: `₹1 = 100 paise`.
- PostgreSQL money columns must use `BIGINT`.
- Never use `float` or `Decimal` for money.
- Every ledger journal must balance.
- Financial writes must use database transactions and row locks.
- Every mutation must be idempotent.
- Every financial and state change must be audited.
- Never change financial state from frontend JavaScript.
- Never treat screenshot or UTR as automatic payment verification.
- Never silently edit ledger history; use reversal journals.
- Business rules belong in service classes, not API routes.
- Secrets and provider credentials belong only in `.env` or an approved secret manager.

---

## 2. Main Product Modules

1. User registration, login and profile
2. Ticket-series management
3. Ticket-package management
4. Ticket purchase and allocation
5. Payment orchestration
6. Result and winner management
7. Wallet and double-entry ledger
8. Withdrawal management
9. User-to-user P2P payment
10. White-label payment gateway
11. Telegram Stars payment
12. Manual QR/UPI payment
13. Coupon, cashback, referral and affiliate management
14. Revenue distribution
15. User dashboard
16. Admin dashboard
17. Notifications and background jobs
18. Audit, dispute and reconciliation

---

## 3. Recommended Project Folder Structure

```text
e_ticket_platform/
├── README.md
├── PROJECT_RULES.md
├── SPEC.md
├── .env.example
├── .gitignore
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── alembic.ini
│
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── dependencies.py
│   ├── exceptions.py
│   ├── logging.py
│   │
│   ├── core/
│   │   ├── security.py
│   │   ├── permissions.py
│   │   ├── idempotency.py
│   │   ├── money.py
│   │   ├── pagination.py
│   │   ├── audit.py
│   │   └── state_machine.py
│   │
│   ├── models/
│   │   ├── user.py
│   │   ├── ticket_series.py
│   │   ├── ticket_package.py
│   │   ├── ticket.py
│   │   ├── order.py
│   │   ├── payment.py
│   │   ├── wallet.py
│   │   ├── ledger.py
│   │   ├── withdrawal.py
│   │   ├── p2p_match.py
│   │   ├── winner.py
│   │   ├── coupon.py
│   │   ├── cashback.py
│   │   ├── referral.py
│   │   ├── affiliate.py
│   │   ├── revenue_allocation.py
│   │   ├── notification.py
│   │   └── audit_log.py
│   │
│   ├── schemas/
│   │   ├── auth.py
│   │   ├── user.py
│   │   ├── ticket_series.py
│   │   ├── ticket_package.py
│   │   ├── order.py
│   │   ├── payment.py
│   │   ├── wallet.py
│   │   ├── withdrawal.py
│   │   ├── result.py
│   │   ├── marketing.py
│   │   └── admin.py
│   │
│   ├── repositories/
│   │   ├── user_repository.py
│   │   ├── ticket_repository.py
│   │   ├── order_repository.py
│   │   ├── payment_repository.py
│   │   ├── wallet_repository.py
│   │   └── withdrawal_repository.py
│   │
│   ├── services/
│   │   ├── auth_service.py
│   │   ├── ticket_series_service.py
│   │   ├── ticket_package_service.py
│   │   ├── inventory_service.py
│   │   ├── order_service.py
│   │   ├── ticket_allocation_service.py
│   │   ├── winner_service.py
│   │   ├── result_service.py
│   │   ├── wallet_service.py
│   │   ├── ledger_service.py
│   │   ├── withdrawal_service.py
│   │   ├── payment_orchestrator.py
│   │   ├── p2p_service.py
│   │   ├── dispute_service.py
│   │   ├── coupon_service.py
│   │   ├── cashback_service.py
│   │   ├── referral_service.py
│   │   ├── affiliate_service.py
│   │   ├── revenue_service.py
│   │   ├── notification_service.py
│   │   └── reconciliation_service.py
│   │
│   ├── payment_adapters/
│   │   ├── base.py
│   │   ├── p2p_adapter.py
│   │   ├── white_label_adapter.py
│   │   ├── telegram_stars_adapter.py
│   │   └── manual_upi_adapter.py
│   │
│   ├── api/
│   │   ├── router.py
│   │   ├── user/
│   │   │   ├── auth.py
│   │   │   ├── profile.py
│   │   │   ├── catalog.py
│   │   │   ├── orders.py
│   │   │   ├── payments.py
│   │   │   ├── tickets.py
│   │   │   ├── results.py
│   │   │   ├── wallet.py
│   │   │   └── withdrawals.py
│   │   ├── admin/
│   │   │   ├── dashboard.py
│   │   │   ├── users.py
│   │   │   ├── ticket_series.py
│   │   │   ├── packages.py
│   │   │   ├── winners.py
│   │   │   ├── payments.py
│   │   │   ├── withdrawals.py
│   │   │   ├── disputes.py
│   │   │   ├── marketing.py
│   │   │   ├── revenue.py
│   │   │   └── audit.py
│   │   └── webhooks/
│   │       ├── white_label.py
│   │       └── telegram.py
│   │
│   ├── tasks/
│   │   ├── celery_app.py
│   │   ├── draw_tasks.py
│   │   ├── notification_tasks.py
│   │   ├── payment_tasks.py
│   │   ├── reconciliation_tasks.py
│   │   └── cleanup_tasks.py
│   │
│   └── templates_and_static/
│       ├── templates/
│       │   ├── public/
│       │   ├── user/
│       │   └── admin/
│       └── static/
│           ├── css/
│           ├── js/
│           └── images/
│
├── alembic/
│   ├── env.py
│   └── versions/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── api/
│   ├── payment/
│   ├── ledger/
│   ├── p2p/
│   └── concurrency/
│
├── scripts/
│   ├── seed_demo_data.py
│   ├── create_admin.py
│   ├── reconcile_ledger.py
│   └── verify_journal_balance.py
│
└── docs/
    ├── architecture.md
    ├── database_schema.md
    ├── api_contract.md
    ├── payment_flows.md
    ├── p2p_state_machine.md
    ├── deployment.md
    └── security_checklist.md
```

---

## 4. User-Facing Website Structure

### 4.1 Public pages

- Home page
- Available ticket-series page
- Ticket-package page
- Ticket-series details
- Result-checking page
- How it works
- Payment methods
- Terms, privacy, refund and responsible-use pages
- Registration and login

### 4.2 User dashboard

- Profile and verified payment destination
- Available ticket series
- Purchased tickets
- Purchased packages
- Order history
- Payment history
- Result and winning history
- Wallet balance
- Available balance
- Locked balance
- Withdrawal request
- Withdrawal status/history
- Referral code and rewards
- Cashback history
- Notifications

### 4.3 Simple ticket purchase flow

1. User selects a ticket series or package.
2. System checks availability and price.
3. Coupon/cashback eligibility is calculated.
4. Order is created with a short reservation period.
5. User selects one available payment method.
6. Payment is verified according to that method.
7. System marks order `PAID` exactly once.
8. Tickets are allocated exactly once.
9. User sees tickets in dashboard.

---

## 5. Admin Panel Structure

### 5.1 Dashboard

- Total users
- Open/closed/drawn series
- Tickets sold and remaining
- Pending payments
- P2P matches and disputes
- Pending manual payments
- Pending withdrawals
- Prize liability
- Revenue-allocation summary
- Provider reconciliation alerts

### 5.2 Ticket-series management

- Create/edit/publish/close/cancel series
- Set ticket price, including ₹30/₹150 or other configured values
- Set limited ticket quantity
- Set sales start/end time
- Set draw time
- Set prize structure
- View sold/unsold inventory
- Prevent unsafe editing after sales begin

### 5.3 Package management

- Create a package containing tickets from multiple series
- Set package price and optional discount
- Set package inventory/availability
- Edit/deactivate package
- Validate availability in every included series

### 5.4 Winner and result management

- Close ticket sales
- Run winner-selection process
- Configure prize ranks and amounts
- Publish result
- Credit prize to winner wallet
- Display winner/result to users
- Keep immutable draw and result audit record

> Admin may configure the prize structure and initiate/approve a draw, but production winner selection should use an independently auditable RNG or seed-commit/reveal process. An admin must not secretly select a preferred winner.

### 5.5 Withdrawal management

- View pending withdrawal requests
- View locked amount and verified destination
- Approve/reject safe manual withdrawal cases
- View P2P-linked withdrawal status
- Handle disputes and late payments
- Preserve full audit and ledger trail

### 5.6 Marketing panel

- Coupon rules
- Referral rewards

- Cashback campaigns
- Affiliate accounts and commissions
- Usage limits and validity dates
- Campaign performance reports

---

## 6. Database Table Structure

### Identity and access

- `users`
- `user_profiles`
- `user_payment_destinations`
- `refresh_tokens`
- `roles`
- `permissions`
- `user_roles`

### Ticket catalog and inventory

- `ticket_series`
- `ticket_series_prizes`
- `ticket_packages`
- `ticket_package_items`
- `tickets`
- `ticket_reservations`
- `draws`
- `winners`

### Orders and payments

- `orders`
- `order_items`
- `payments`
- `payment_attempts`
- `payment_events`
- `provider_webhook_events`
- `manual_payment_proofs`

### Wallet and accounting

- `wallets`
- `ledger_accounts`
- `journal_groups`
- `journal_entries`
- `wallet_holds`
- `withdrawals`
- `refunds`
- `reversals`
- `revenue_allocations`

### P2P

- `p2p_matches`
- `p2p_payment_proofs`
- `p2p_receiver_decisions`
- `p2p_disputes`
- `p2p_events`

### Marketing

- `coupons`
- `coupon_redemptions`
- `referrals`
- `referral_rewards`
- `cashback_campaigns`
- `cashback_rewards`
- `affiliates`
- `affiliate_clicks`
- `affiliate_conversions`
- `affiliate_commissions`

### Operations

- `notifications`
- `idempotency_records`
- `audit_logs`
- `outbox_events`
- `reconciliation_runs`
- `system_settings`

---

## 7. Important Database Fields

### `ticket_series`

- `id`
- `name`
- `description`
- `price_paise BIGINT`
- `ticket_limit`
- `sold_count`
- `reserved_count`
- `sales_start_at`
- `sales_end_at`
- `draw_at`
- `status`
- `created_by`
- timestamps

### `tickets`

- `id`
- `series_id`
- `serial_number`
- `order_item_id`
- `owner_user_id`
- `status`
- `is_winner`
- `prize_paise BIGINT`
- timestamps
- unique constraint on `(series_id, serial_number)`

### `wallets`

- `user_id`
- `available_paise BIGINT`
- `locked_paise BIGINT`
- `version`
- timestamps

### `payments`

- `id`
- `order_id`
- `method`
- `amount_paise BIGINT`
- `status`
- `provider_reference`
- `matched_withdrawal_id`
- `metadata_json`
- timestamps

### `withdrawals`

- `id`
- `user_id`
- `amount_paise BIGINT`
- `destination_snapshot_json`
- `status`
- `receiver_confirmed`
- timestamps

### `journal_entries`

- `id`
- `journal_group_id`
- `account_id`
- `side` (`DEBIT` or `CREDIT`)
- `amount_paise BIGINT`
- timestamps

Each journal group must satisfy:

```text
total_debit_paise == total_credit_paise
```

---

## 8. State Machines

### Ticket series

```text
DRAFT -> PUBLISHED -> OPEN -> CLOSED -> DRAWN -> RESULT_PUBLISHED
                       |        |
                       +------> CANCELLED
```

### Order

```text
DRAFT -> PENDING_PAYMENT -> PAYMENT_REVIEW -> PAID -> FULFILLED
              |                    |            |
              +-----------------> CANCELLED     +-> REFUNDED
```

### Payment

```text
CREATED -> AWAITING_PAYMENT -> PROOF_SUBMITTED/PROVIDER_CONFIRMED
                                      |
                            UNDER_REVIEW/DISPUTED
                                |             |
                            SUCCEEDED       FAILED
```

### Withdrawal

```text
REQUESTED -> FUNDS_LOCKED -> OPEN -> MATCHED -> PROOF_SUBMITTED
                                |                    |
                             CANCELLED      COMPLETED or DISPUTED
```

---

## 9. User-to-User P2P Payment Structure

The existing **P2P Payment Specification v1.2** is the source of truth.

### Participants

- User A: withdrawal/receiver user
- User B: buyer/payer user

### Locked rules

- User A can request at most 50% of eligible pre-hold available balance:

```text
eligible_paise = floor(available_paise * 5000 / 10000)
```

- Minimum withdrawal is configurable; initial requirement is ₹50 (`5000` paise).
- Withdrawal creation moves money from `available` to `locked`.
- One user can have only one unresolved withdrawal.
- User B's order must match the exact withdrawal amount.
- V1 has no partial or split matching.
- The buyer receives a frozen snapshot of User A's verified payment destination.
- Buyer submits UTR/reference and proof.
- Proof is not automatic verification.
- User A selects `Payment Received` or `Not Received`.
- Settlement occurs only after required verification and confirmation.
- Settlement debits locked wallet amount exactly once.
- Order becomes `PAID` and ticket/product delivery happens idempotently.

### P2P safety rules

- Do not automatically unlock after payment details are exposed.
- Missing screenshot does not prove non-payment.
- Receiver denial creates a dispute.
- Late payment remains linked to the original match.
- Wrong amount, duplicate UTR or provider uncertainty requires manual review.
- Refund and wallet hold release are different operations.
- Use `SELECT FOR UPDATE` or equivalent row locks during matching/settlement.

---

## 10. White-Label Payment Structure

### Purpose

Connect an approved white-label merchant/payment provider without mixing provider code with core business logic.

### Required components

- Provider adapter interface
- Create-payment method
- Check-status method
- Refund method, if provider supports it
- Webhook-signature verification
- Provider-event idempotency
- Payment reconciliation job
- Provider reference mapping
- Retry and timeout policy

### Flow

1. User creates an order.
2. Backend creates a provider payment request.
3. Provider returns payment URL/intent/QR.
4. User pays on provider-approved flow.
5. Signed webhook reaches backend.
6. Backend stores raw event once.
7. Signature and amount are verified.
8. Order is settled exactly once.
9. Tickets are allocated exactly once.

Do not simulate or fake provider confirmation.

---

## 11. Telegram Stars Payment Structure

### Required configuration

- Telegram bot token
- Bot username
- Webhook URL
- Webhook secret
- Product/order payload mapping
- Stars price configuration

### Flow

1. Backend creates an order-linked Telegram invoice payload.
2. User opens and pays the invoice through Telegram.
3. Telegram sends pre-checkout and successful-payment updates.
4. Backend validates bot/webhook secret and payload.
5. Telegram charge ID is stored uniquely.
6. Backend marks payment successful exactly once.
7. Ticket/product is delivered exactly once.

Telegram Stars must be used only for products and flows permitted by Telegram's current rules.

---

## 12. Manual QR/UPI Payment Structure

### Flow

1. Admin configures approved UPI ID and QR.
2. User creates an order.
3. Backend freezes order amount and expiry.
4. User pays and uploads screenshot plus UTR/reference.
5. System checks duplicate UTR but does not auto-approve it.
6. Payment appears in admin review queue.
7. Admin/provider evidence confirms or rejects payment.
8. Approved payment activates the order/product exactly once.
9. Rejected or uncertain payment moves to review/dispute, not silent deletion.

### Required records

- Order ID
- User ID
- Expected amount in paise
- UPI destination snapshot
- User-entered UTR
- Proof file reference
- Admin decision
- Decision note
- Reviewer ID
- Timestamps and audit log

---

## 13. Revenue Distribution Structure

For each successfully settled eligible order:

| Allocation | Percentage |
|---|---:|
| Prize pool | 45% |
| Marketing | 20% |
| Operations | 10% |
| Emergency reserve | 5% |
| Profit and growth fund | 20% |
| Total | 100% |

### Calculation rule

- Calculate in integer paise only.
- Allocate each component deterministically.
- Put any rounding remainder into the configured remainder account, recommended: profit/growth fund.
- Create balanced ledger entries.
- Never recalculate or overwrite a posted allocation silently.

---

## 14. Core API Structure

### Authentication

- `POST /api/v1/auth/register`
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/refresh`
- `POST /api/v1/auth/logout`

### Catalog and orders

- `GET /api/v1/ticket-series`
- `GET /api/v1/ticket-series/{id}`
- `GET /api/v1/ticket-packages`
- `POST /api/v1/orders`
- `GET /api/v1/orders/{id}`
- `POST /api/v1/orders/{id}/payments`

### User tickets/results

- `GET /api/v1/me/tickets`
- `GET /api/v1/me/orders`
- `GET /api/v1/results`
- `GET /api/v1/results/{series_id}`

### Wallet and withdrawal

- `GET /api/v1/me/wallet`
- `GET /api/v1/me/transactions`
- `POST /api/v1/withdrawals`
- `GET /api/v1/withdrawals`
- `POST /api/v1/withdrawals/{id}/receiver-decision`

### Payment proof

- `POST /api/v1/payments/{id}/proof`
- `GET /api/v1/payments/{id}`

### Webhooks

- `POST /api/v1/webhooks/white-label/{provider}`
- `POST /api/v1/webhooks/telegram/{secret}`

### Admin

- Series CRUD and lifecycle endpoints
- Package CRUD endpoints
- Payment-review endpoints
- Withdrawal-review endpoints
- Dispute endpoints
- Draw and result endpoints
- Coupon/cashback/referral/affiliate endpoints
- Revenue and reconciliation reports
- Audit-log endpoints

All mutation APIs must require authentication, authorization and an idempotency key where applicable.

---

## 15. Background Jobs

- Close series when sales period ends
- Release only safe, unexposed reservations
- Run approved draw workflow
- Publish results
- Send purchase/result/withdrawal notifications
- Retry provider status checks safely
- Reconcile provider settlements
- Detect duplicate references
- Generate finance and allocation reports
- Monitor stuck P2P matches and disputes
- Archive expired non-financial data according to retention policy

---

## 16. Test Structure

### Money and ledger tests

- Reject float/Decimal money input
- Verify paise conversion
- Every journal balances
- Reversal journal correctness
- Revenue percentages sum to exact order total

### Ticket tests

- Ticket limit cannot be exceeded
- Concurrent buyers cannot receive the same serial number
- Payment settlement allocates tickets once
- Package correctly allocates every included series
- Cancelled/failed payment receives no ticket

### Winner tests

- Only sold tickets can win
- A ticket cannot win the same unique rank twice
- Prize credit is idempotent
- Draw audit is immutable
- Result publication is authorized

### P2P tests

- 50% eligibility uses integer floor
- One unresolved withdrawal per user
- Exact-amount matching only
- Self-match blocked
- Concurrent orders cannot claim the same withdrawal
- Duplicate UTR blocked/reviewed
- Receiver denial creates dispute
- Settlement debits locked funds exactly once
- Timer never unlocks exposed payment automatically
- Late payment remains linked to original match

### Payment tests

- Invalid webhook signature rejected
- Duplicate webhook is idempotent
- Amount mismatch goes to review
- Manual screenshot alone never settles
- Telegram charge ID cannot be reused
- Provider failure does not activate tickets

---

## 17. Codex Implementation Order

### Phase 1 - Foundation

1. Create repository and Docker Compose.
2. Add FastAPI, PostgreSQL, Redis and Celery.
3. Add configuration, logging, authentication and RBAC.
4. Add Alembic and base database models.

### Phase 2 - Financial core

1. Implement integer-paise money validation.
2. Implement wallet, accounts, journals and entries.
3. Implement balanced journal validator.
4. Implement idempotency and audit logs.
5. Add concurrency tests before payment work.

### Phase 3 - Tickets and orders

1. Series and prize configuration.
2. Limited ticket inventory.
3. Packages with multi-series items.
4. Order creation and reservation.
5. Ticket allocation after settlement.

### Phase 4 - P2P

Implement the existing P2P Specification v1.2 exactly, including matching, holds, proof, confirmation, dispute, late-payment and settlement rules.

### Phase 5 - Other payment methods

1. White-label adapter boundary
2. Telegram Stars integration
3. Manual QR/UPI review
4. Provider webhook and reconciliation framework

### Phase 6 - Winner/result system

1. Sales closure
2. Auditable winner selection
3. Prize posting
4. Result publication
5. User notifications

### Phase 7 - Marketing and admin

1. Coupons
2. Referrals
3. Cashback
4. Affiliates
5. Revenue reports
6. Admin review/dispute tools

### Phase 8 - Frontend

1. Public catalog
2. Two-to-three-step purchase flow
3. User dashboard
4. Wallet and withdrawal pages
5. Result page
6. Admin dashboard
7. Mobile responsiveness and accessibility

### Phase 9 - Local production-hardening implementation

Implementation scope (completed locally; not a production release approval):

1. Fail-closed production configuration and defensive response headers.
2. Separate liveness and bounded database/broker readiness endpoints.
3. Read-only ledger and wallet integrity tooling.
4. Administrator-only, redacted and bounded audit metadata reads.
5. Operational runbook, security checklist, and a test report that separates local results from release readiness.

Scope clarification (2026-09-25): verification and external certification are not
Phase 9 implementation-completion criteria. Prior local test evidence remains
historical; this clarification does not claim a new test run or production approval.
The original Phase 9 launch items remain mandatory before live operation:

1. Independent security review.
2. Legal/compliance approval.
3. Provider certification.
4. PostgreSQL load and race-condition tests.
5. Backups and disaster-recovery drill.
6. Monitoring, alerts, and operational ownership.
7. Independent draw/fairness audit.

These pre-launch gates are tracked in `docs/security_checklist.md`, outside the
implementation phase. Completing Phase 9 does not satisfy the project-wide
Definition of Done or authorize live payments or deployment.

---

## 18. Definition of Done

The project is not complete until:

- All money is integer paise.
- Every journal balances.
- Ticket inventory survives concurrency tests.
- All mutations are idempotent.
- P2P follows v1.2 without shortcuts.
- Screenshots/UTRs never auto-verify payment.
- Payment webhooks are signed and deduplicated.
- Orders and ticket delivery settle exactly once.
- Winner selection and prize credit are auditable.
- All admin actions are logged.
- Four payment methods have separate adapters and state handling.
- User and admin dashboards work on mobile.
- Tests cover success, failure, duplicate, timeout, dispute and concurrency cases.
- Production launch has documented legal and payment-provider approval.
