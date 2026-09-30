# P2P Payment and Withdrawal Matching System

Version: 1.2
Status: Corrected implementation specification
Scope: Payment / withdrawal section only

This version supersedes conflicting rules in versions 1.0 and 1.1. It defines engineering behavior, not approval of any payment provider or business model. Real-money integration depends on selecting a provider that supports the actual proposed flow. Until configured, use a clearly labelled sandbox adapter; never simulate verification in production.

## 1. Purpose and roles

The platform matches a product buyer with a user requesting withdrawal of an existing virtual-wallet balance. The buyer pays the receiver's assigned UPI destination directly. Verified settlement reduces the receiver's wallet liability and pays the buyer's order. Product delivery follows settlement.

- User A / receiver: requests withdrawal and confirms receipt in their bank/payment account.
- User B / buyer: purchases a platform product, pays the assigned receiver, and submits payment details.
- Admin: monitors, reconciles, and resolves disputes with evidence and an audit trail.

The buyer does not receive a withdrawal. The receiver receives external money; the platform wallet is reduced, not credited by that payment. A wallet balance alone does not demonstrate funds or establish how its original credit was earned; originating credits are supplied by the host platform and must be traceable.

## 2. Authoritative Version 1 defaults

| Setting | Default / rule |
|---|---|
| Currency | INR, integer paise |
| Withdrawal percentage | 50%, represented as 5000 basis points |
| Full-balance threshold override | Disabled |
| Concurrent unresolved withdrawals per user | Exactly one maximum |
| Matching | Exact requested amount only |
| Split matching | Excluded |
| Buyer payment window | 15 minutes; configurable |
| Receiver confirmation window | 30 minutes after submission; configurable |
| Automatic approval on timer expiry | Never |
| Automatic release merely because proof is missing | Never after payment details were exposed |
| Product delivery | Idempotent, after settlement |

Timeouts are initial engineering defaults, not guaranteed resolution SLAs. Production amount limits, fees, provider integration, refund funding and operating SLAs must be configured before real-money operation. V1 sandbox calculations assume zero fees; do not silently introduce fees into exact matching.

## 3. Eligibility and the 50% rule

Let B be eligible available balance immediately BEFORE placing this request's hold. B excludes other reservations and ineligible credits. Let P be the snapshotted withdrawal percentage in basis points.

```text
max_amount = floor(B * P / 10000)
if threshold_override_enabled and B <= threshold_paise:
    max_amount = B
max_amount = min(max_amount, configured_max_withdrawal)
allow only if configured_min_withdrawal <= requested_amount <= max_amount
```

All quantities are integer minor units. Requested amount must be positive. If the calculated maximum is below the minimum, show no eligible withdrawal; do not round upward.

At default P = 5000, X <= floor(B / 2) is equivalent to B >= 2*X. The 2x statement is explanatory, NOT an additional independent rule. A percentage change or threshold exception must use the same authoritative eligibility function everywhere.

The maximum is a ceiling. A user with ₹2,000 may request ₹500 even though ₹1,000 is permitted. The engine must not force the maximum or resize an existing request automatically.

Creation runs under a wallet lock, validates no unresolved withdrawal exists, snapshots B and the rule, and reserves X atomically. New rule versions apply only to new requests.

### Match-time validation

Validate the stored eligibility snapshot and rule version, the request's own live reservation of X, current user/payment-handle eligibility, and request status. Do NOT apply the percentage to post-hold available balance again.

Example: B = ₹1,000, X = ₹500. After holding, available is ₹500 and locked is ₹500. This remains valid. Testing ₹500 >= 2*₹500 at matching is a bug.

If the reservation is missing or inconsistent, stop and send to review. Do not silently take a new hold. Subsequent deposits or spending do not recalculate the request's snapshotted ceiling. V1 does not reserve the remaining half as collateral; any requirement to keep that half unspendable is a separate business rule, not implied by 2x eligibility.

### Default-mode examples

| Pre-hold eligible balance | Maximum | Requested amount / buyer price | Result |
|---:|---:|---:|---|
| ₹2,000 | ₹1,000 | ₹500 | Allow if an actual ₹500 request exists |
| ₹2,000 | ₹1,000 | ₹1,000 | Allow |
| ₹2,000 | ₹1,000 | ₹1,500 | Reject |
| ₹300 | ₹150 | ₹250 | Reject |
| ₹500 | ₹250 | ₹250 | Allow |
| ₹1,000 | ₹500 | ₹250 | Allow |

Threshold tests are separate: ONLY when an explicit override of ₹500 is enabled, B = ₹300 may permit X = ₹250. Default examples must never run with that override enabled.

## 4. End-to-end example

1. A has ₹1,000 available and no unresolved withdrawal.
2. A requests ₹500. Save the rule/balance snapshot and reserve ₹500.
3. Wallet displays ₹1,000 total, ₹500 available, ₹500 locked. Further withdrawal requests are blocked.
4. B creates a ₹500 product order. The server locks and links the order and A's open ₹500 withdrawal.
5. B sees the assigned receiver, exact amount, deadline and transaction ID. Freeze the payment destination for this attempt.
6. B pays A externally and submits UTR/reference, declared payment time and supporting proof.
7. A confirms received or not received. Verification collects independently supported evidence; proof alone cannot complete settlement.
8. On normal verified confirmation, or evidenced admin resolution, settle once in the database.
9. A's locked ₹500 is debited; wallet becomes ₹500 available and zero locked. Withdrawal completes. B's order becomes PAID, with DELIVERY_PENDING.
10. Deliver the product asynchronously and idempotently. Only successful fulfillment becomes DELIVERED/ACTIVATED.
11. A may now request up to ₹250 under the default rule. If the prior request is disputed or awaiting reconciliation, the next request remains blocked.

## 5. Wallet and balanced journal

Expose available_balance, locked_balance, total_balance = available + locked. Reservations identify which request owns locked funds. Store immutable journal transactions and balanced posting lines; cached balances must reconcile to them.

Suggested logical accounts (credit-normal liabilities): USER_AVAILABLE, USER_WITHDRAWAL_HELD, ORDER_FUNDS_PENDING_FULFILLMENT, BUYER_REFUND_PAYABLE. Journal groups include event type, reference, currency, actor, reason, timestamps and idempotency key. Posting lines include account ID, debit or credit, and positive amount.

| Event | Debit | Credit |
|---|---|---|
| Hold X | A available liability X | A held liability X |
| Release X | A held liability X | A available liability X |
| Settle X | A held liability X | B order funds pending fulfillment X |
| Refund obligation after settlement | Order funds pending fulfillment, or appropriate reversal account if already recognized | Buyer refund payable |

These are operational postings, not a complete revenue/tax accounting policy. Revenue recognition and original wallet credits belong to the host platform's accounting integration. Never create a fictitious platform cash receipt for money sent directly from B to A. Refund payout debits refund payable and credits the actual funded payout account only after verified payment.

Every journal group must balance within one currency. Enforce nonnegative wallet balances and one settlement/release event per applicable request. Corrections use linked reversal postings, never edits or deletes. Settled-payment refunds do not automatically restore A's wallet; A already received money externally. Receiver recovery, if needed, requires a separate evidenced transaction.

## 6. Matching and reservations

Match the actual order total against the actual open withdrawal amount and currency. Both users must be eligible, different users, and pass configured risk checks. Shared IP/device signals warrant risk review; they alone do not establish identity.

Select eligible requests oldest-first with a deterministic tie-break. Lock order, request and reservation in a consistent order. Atomically claim them. Database uniqueness must prevent two active matches for one order or withdrawal.

An existing ₹1,000 request cannot match a ₹500 order in V1. Before assignment/payment exposure, the receiver may cancel and create a fresh ₹500 request with fresh eligibility evaluation. Never partially settle, automatically resize, or silently replace exposed payment details.

When no request matches, the order remains waiting without showing an unassigned payment destination. Do not promise instant availability.

## 7. States and transitions

Keep separate states for withdrawal, match, payment verification, order and delivery. A combined UI timeline may aggregate them; do not overload one enum.

- Withdrawal: WAITING_FOR_BUYER, MATCHED, UNDER_REVIEW, COMPLETED, CANCELLED.
- Match: WAITING_FOR_PAYMENT, PAYMENT_SUBMITTED, WAITING_FOR_RECEIVER_CONFIRMATION, UNDER_VERIFICATION, EXPIRED_AWAITING_RECONCILIATION, DISPUTED, ADMIN_REVIEW, SETTLED, CLOSED_UNPAID, REFUND_PENDING, REFUNDED.
- Order: WAITING_FOR_MATCH, AWAITING_PAYMENT, PAID, CANCELLED, REFUND_PENDING, REFUNDED.
- Delivery: NOT_STARTED, PENDING, PROCESSING, DELIVERED, FAILED.

Normal progress: reserve and queue -> match -> expose details -> payment submission -> confirmation/verification -> settle -> delivery.

| Transition | Preconditions / actor | Money effect |
|---|---|---|
| Create withdrawal | Eligible authenticated receiver, no unresolved request | Hold once |
| Assign match | Exact match, valid hold, atomic server claim | None |
| Submit payment | Buyer owns order; persist evidence and reference | None |
| Confirm receipt | Assigned receiver; authenticated server action | None |
| Settle | Verified payment + receiver confirmation OR evidenced admin override | Held liability settles once |
| Unmatched cancellation | No payment destination exposed or unresolved payment | Release once |
| Payment deadline expires | Background worker | Review state, no release |
| Close unpaid | Supported reconciliation or evidenced admin decision | Keep hold for safe rematch, or release if cancelling |
| Delivery retry | Order paid, entitlement absent | No new settlement |

Out-of-order submissions/webhooks must be retained and processed against authoritative facts. A late event cannot regress SETTLED to WAITING. If a late payment arrives after a match was closed, create an exception for the ORIGINAL match; never silently attach it to a new buyer.

## 8. Verification and confirmation

Capture internal IDs, expected and observed amount/currency, assigned recipient snapshot, provider reference/UTR, declared payment time, upload ID, provider evidence and receiver decision. An entered UTR or screenshot is a claim, not verification.

The adapter must actually support verification of the selected recipient's transactions. Do not assume a generic gateway can query arbitrary personal-UPI transfers. Where authoritative integration is unavailable, use an explicit manual reconciliation workflow rather than a fake successful API result.

Normal completion requires supported verification plus receiver confirmation. If the receiver refuses or does not respond, authorized admin may resolve with independent evidence, reason and recorded override. A buyer screenshot alone or elapsed timer is insufficient. Use maker-checker review for configured high-risk overrides.

Scope reference uniqueness by the provider's documented identity namespace, e.g. provider/network plus transaction identifier. Exact retries for the same match return the existing result; attempted reuse across matches triggers review. Invalid claims must not permanently poison ownership of a genuine reference without adjudication.

Signed webhooks require signature checking, replay protection, durable event capture, deduplication and retry-safe processing. Conflicting evidence enters review.

## 9. Timeout, cancellation and late payments

- Before payment details are exposed, an unmatched cancellation can release its hold atomically.
- After exposure, missing proof does not prove nonpayment. Buyer may have paid and lost connectivity.
- Deadline expiry enters EXPIRED_AWAITING_RECONCILIATION. Disable further payment instructions and retain hold.
- Check authoritative evidence, pending provider events and submitted claims. If no definitive automated check exists, require documented admin reconciliation.
- Rematch or release only after a supported unpaid closure. Preserve the old attempt and its recipient/reference history.
- Receiver timeout moves to review, never automatic success or rejection.
- Late payment/proof remains attributable to the original attempt. Stop automatic processing and resolve delivery/refund as appropriate.
- Timers and worker retries are durable and idempotent; server time is authoritative.

UPI instructions may remain usable outside the app after expiry. The system cannot equate hiding a QR with preventing an external transfer.

## 10. Fault handling and user responses

| Case | Required outcome |
|---|---|
| Request exceeds ceiling / invalid amount | Reject before hold; show eligible maximum |
| Another unresolved withdrawal | Reject second request; link existing request |
| Wrong paid amount | AMOUNT_MISMATCH review reason; retain evidence; no auto-settlement |
| Duplicate reference across matches | Review; never settle twice |
| Receiver denies receipt | DISPUTED, retain hold |
| Provider pending / uncertain | Remain pending; no delivery or automatic release |
| Provider failure | Reconcile all evidence before unpaid closure |
| Suspected false proof / collusion | Risk review with evidence |
| Server error during action | Return retriable response with same idempotency key |
| Settlement committed, response lost | Retry returns existing settlement |
| Delivery fails | Paid order remains paid; retry fulfillment, never charge again |
| Reversal after settlement | Post-settlement exception; no blind wallet restoration |
| No exact receiver available | Waiting state; no invented destination |

Receiver success: “₹500 withdrawal completed. Remaining wallet balance: ₹500.”
Buyer paid, delivery pending: “Payment verified. Your product activation is pending.”
Buyer fulfilled: “Your product has been activated.”
Pending: “Payment verification is pending. Your transaction is being tracked.”
Dispute: “Payment details require review. Product activation remains pending.”

## 11. Refund and post-settlement resolution

A wallet hold release is NOT a refund of an external UPI payment.

Each refund case records original match, verified original receipt, amount/currency, reason, liable party, approved funding source, destination validation, executor, payout reference and verified status. No automatic refund to a destination supplied only in a new chat/message.

Before settlement: retain the original hold while resolving paid-but-invalid transactions. Close/refund only after payout evidence is verified; then release the hold if the original withdrawal will not settle. If settlement already occurred, preserve its history and record a separate refund obligation/payout. Do not debit or credit the receiver again without a separately supported reason.

The production integration must choose who executes refunds: an approved provider route or a documented manual refund operator with an actual funding source. Do not claim the platform can reverse a direct transfer on command. Partial refunds, reversal recovery and entitlements already consumed require explicit operator decisions; leave such cases in review until resolved.

## 12. Data entities and database constraints

- users: identity, eligibility, verification and risk status.
- wallets: user, currency, cached available/locked balances, version.
- wallet_reservations: request, amount, status, timestamps.
- journal_transactions: event, idempotency key, actor, reason, source, reversal link.
- journal_postings: journal, account, debit/credit, amount, currency.
- payment_handles: owner, verified recipient details, verification method/version.
- withdrawal_requests: user, amount, eligible_balance_snapshot, max_snapshot, rule_snapshot/version, status.
- purchase_orders: buyer, immutable product/price snapshot, amount, currency, payment status.
- payment_matches: order, withdrawal, frozen destination snapshot, status, deadlines, exposure time.
- payment_submissions: match, claimed reference, attachment, observed fields, verification result.
- provider_events: external event ID, verified payload digest, processing status.
- receiver_confirmations: match, actor, decision, time.
- disputes/admin_resolutions: evidence, reason, decision, actor, override path.
- settlements: unique withdrawal/match settlement and journal link.
- delivery_jobs/entitlements: order, product, status, attempts, unique fulfillment key.
- refunds: original match/settlement, funding/executor details, amount, payout reference, status.
- outbox_events: durable event, unique key, delivery/retry status.
- audit_logs: actor, entity, transition, reason, timestamp, safe metadata.

Enforce one unresolved withdrawal per user, one active match per order/request, one live reservation per request, unique settlement and unique product entitlement per order. Use foreign keys, positive-amount checks, consistent currency and transaction locks. All transitions and ownership checks are backend-enforced.

## 13. Atomic operations and idempotency

Creation: lock wallet -> validate eligibility/unresolved requests -> save snapshot -> reserve -> post balanced hold journal -> update cached balances -> commit.

Settlement: lock related records -> validate evidence and valid hold -> insert unique settlement -> post balanced journal -> consume reservation -> complete withdrawal -> mark order PAID and delivery PENDING -> insert outbox event -> commit.

External money movement and product delivery cannot be part of the same database transaction. Use durable reconciliation and transactional outbox processing. A failed delivery cannot roll back real payment.

Scope mutation idempotency keys by actor/action. Store request fingerprint and response. Same key/same payload returns the original result; same key/different payload is rejected. Workers and callbacks use stable unique external/internal event IDs. Do not hold database locks while waiting on external APIs.

## 14. API contract outline

| Endpoint | Responsibility |
|---|---|
| POST /withdrawals | Validate, snapshot and reserve |
| GET /withdrawals/{id} | Owner/admin status and timeline |
| POST /withdrawals/{id}/cancel | Safe cancellation rules |
| POST /orders | Server-side price snapshot and matching queue |
| GET /matches/{id} | Authorized immutable payment instructions/status |
| POST /matches/{id}/payment-submissions | Evidence and UTR claim, including late submissions |
| POST /matches/{id}/receiver-confirmation | Assigned receiver decision |
| POST /payments/provider-webhook | Verified durable provider event ingestion |
| POST /admin/disputes/{id}/resolve | Evidence-based resolution |
| POST /admin/matches/{id}/close-unpaid | Reconciled closure/rematch decision |
| POST /admin/orders/{id}/retry-delivery | Retry without duplicate entitlement |
| POST /admin/refunds | Create controlled refund case |

Require authentication except signed provider callbacks, ownership/role checks, input validation, idempotency and audits. Never accept a buyer-supplied price or arbitrary settlement status. Useful errors: INSUFFICIENT_ELIGIBLE_BALANCE, ACTIVE_WITHDRAWAL_EXISTS, NO_EXACT_MATCH, INVALID_TRANSITION, REFERENCE_CONFLICT, IDEMPOTENCY_CONFLICT, REVIEW_REQUIRED.

## 15. Admin, security and monitoring

Dashboard: participants, product/order, amount, recipient snapshot, wallet before/after, reservation, rule snapshot, verification source, UTR/proof, confirmation, timeline, deadlines, dispute/refund status, delivery state and journal links.

Actions must follow the same transitions as public APIs; no unrestricted “set completed” or balance-edit button. Require a reason and evidence for overrides. Mask sensitive details, use restricted evidence access, enforce upload type/size checks and scan uploads. Protect secrets, rate-limit mutations, and audit all access to sensitive evidence.

Monitor aged holds, expired-unreconciled matches, provider event failures, settlement conflicts, delivery retries and reconciliation differences. Daily reconciliation checks balances against journals, holds against reservations, paid orders against settlements, and delivered products against entitlements. Alert on mismatches; do not silently repair financial records.

Notify both parties with role-specific information at matching, submission, confirmation request, review, settlement and delivery. Notification failure must not reverse settlement; retry through the outbox.

## 16. Required acceptance tests

1. Default ₹1,000 balance allows ₹500, rejects ₹500.01; hold yields ₹500 available/₹500 locked.
2. ₹2,000 balance permits a ₹500 request; maximum is not mandatory.
3. ₹300 balance rejects ₹250 under default rules; ₹500 permits ₹250.
4. Valid ₹500 hold from ₹1,000 still matches after available drops to ₹500.
5. Two concurrent withdrawal creations produce only one unresolved request and one hold.
6. No new withdrawal while prior request is pending/disputed/unreconciled. After settlement, next ceiling uses current pre-hold balance.
7. Two buyers racing for one request produce exactly one match.
8. Existing ₹1,000 request never matches ₹500 order without explicit safe replacement.
9. Threshold disabled vs explicitly enabled tests use different expected outcomes.
10. A changed percentage uses the formula; no hard-coded 2x check contradicts it. Existing requests retain their rule snapshot.
11. Missing proof at deadline enters reconciliation and does not release/rematch automatically.
12. Late payment links to original attempt and triggers controlled review.
13. Screenshot/UTR claim alone cannot settle. Receiver timeout alone cannot settle.
14. Evidenced admin resolution can resolve receiver nonresponse with a recorded override.
15. Repeated confirmation/webhook/API retries debit once and create one entitlement.
16. Failed settlement transaction leaves no partial journals, balances, order updates or outbox events.
17. Delivery failure leaves PAID/FAILED delivery status; retry does not debit again.
18. All journal groups balance; available/locked cached balances reconcile.
19. Hold release is not reported as an external refund. Refunded status requires verified payout evidence.
20. Unauthorized actors cannot view proof, change recipient, confirm another receiver's match or resolve disputes.

## 17. Implementation boundaries

Implement the defaults above without adding split matching, investment returns, referral mechanics, multi-currency or cross-border flows. Use adapters for the host platform's wallet-credit source, authentication, products and delivery.

Before enabling real-money operation, configure actual provider capabilities, receiver verification, amount limits, fees, refund responsibility/funding, business eligibility and evidence/retention policies. The previously raised legal/provider review remains an external launch dependency, not something code or this document certifies.

## 18. Changes from 1.1

- Strict 50% default and disabled threshold remove contradictory examples.
- 2x becomes an explanation rather than a second hard-coded validator.
- Pre-hold snapshot prevents false rejection at matching.
- One unresolved withdrawal enforces sequential withdrawal cycles.
- Actual request matching is distinguished from theoretical wallet eligibility.
- Exposed-payment expiry requires reconciliation, not immediate release.
- Evidenced admin resolution handles receiver refusal/nonresponse.
- Payment settlement and delivery have independent statuses.
- Balanced journals, refund funding and immutable history are specified.
- Concurrency, late-event handling and meaningful acceptance tests are explicit.
