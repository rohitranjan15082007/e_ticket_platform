# Payment flows

## Implemented P2P accounting flow

The P2P flow is a liability transfer, not a wallet top-up. A buyer pays the receiver outside the platform using the frozen verified destination. That external claim is not trusted until verification policy permits settlement.

| Stage | Wallet / ledger effect | Order and delivery effect |
|---|---|---|
| Withdrawal request | Debit user available liability; credit user withdrawal-held liability. Cache moves from available to locked. | None. |
| Exact match / proof / confirmation | No journal and no balance change. Evidence and decisions are retained. | Order waits for payment/review. |
| Signed provider callback | No journal or balance change. The raw HMAC-authenticated callback is retained with a unique external event ID and payload digest. | Exact match/reference/amount/currency/recipient facts are verified or routed durably to review. |
| Verified settlement | Debit user withdrawal-held liability; credit `platform:p2p-order-funds-pending:inr`. The named hold becomes `SETTLED`. The same transaction allocates that pending value to five revenue buckets. | Order becomes `PAID`, delivery becomes `PENDING`, and a durable outbox event is recorded. |
| Outbox delivery / retry | No wallet or settlement journal change. | Allocation runs once; successful delivery becomes `FULFILLED`/`DELIVERED` and emits a durable in-app notice. Failure remains `PAID`/`FAILED`. |
| Evidenced unpaid closure | Admin chooses rematch with hold retained, or a one-time release with order cancellation. | No automatic decision follows a timeout. |
| Refund case | No wallet release, receiver debit, payout, or journal mutation. | A post-settlement case is `PENDING_EVIDENCE` and moves match/order into refund review. |

All monetary quantities are integer paise in INR. Every journal group is balanced and linked to an idempotency record; corrections require new linked entries rather than edits.

## Implemented Phase 5 non-P2P payment flow

The Phase 5 path is separate from P2P. The order value remains frozen in integer INR paise; Telegram Stars provider units remain `XTR` and are never converted into a journal amount without the frozen order value.

| Stage | Trusted state / accounting effect | Order and delivery effect |
|---|---|---|
| Start payment attempt | Freeze order amount, provider units, merchant reference, expiry, and method instructions under a buyer idempotency key. White-label initiation remains unavailable without a concrete approved provider adapter. | `PENDING_PAYMENT -> AWAITING_PAYMENT`. |
| Manual UPI proof | Persist a UTR/proof reference with a unique UTR lock. A proof, including late evidence, enters `UNDER_REVIEW`; it is never a settlement. | Order enters or remains `PAYMENT_REVIEW`. |
| White-label callback | Verify raw-body HMAC, retain a provider event/digest once, require exact frozen amount/currency and a unique nonblank provider payment reference. Ambiguity is review-only. | Matching trusted success may settle; other evidence remains review-only. |
| Telegram pre-checkout | Verify header secret, signed invoice payload, Telegram user, `XTR` amount, and update replay digest. A bounded acknowledgement answers the query but is not payment proof. | No settlement. |
| Telegram successful payment | Verify the distinct successful-payment update and unique Telegram charge ID. | Matching trusted success may settle once. |
| Trusted settlement | Create one balanced journal from clearing to `platform:external-order-pending:inr`, then allocate that pending value to five revenue buckets in another balanced journal in the same transaction. Record one settlement, audit history, and one generic outbox event. | Order becomes `PAID`; ticket delivery is pending. |
| Generic delivery outbox | Retry only settlement-gated ticket allocation. It does not send notifications or change a settlement. | Successful allocation fulfills delivery; a failure stays paid and retryable. |
| Expiry and reconciliation | Expired instructions and uncertain events are retained for reconciliation. The scheduled reconciliation service writes review-queue snapshots only. | No automatic release, refund, provider polling, bank matching, or auto-settlement. |

Manual UPI late-proof handling is deliberately scheduler-independent: evidence received after `expires_at` is retained for accountable administrator review whether it arrives before or after the expiry worker. An administrator may resolve exact evidence explicitly; there is no automatic settlement path.

## Implemented Phase 6 draw and prize flow

| Stage | Trusted state / accounting effect | Series and result effect |
|---|---|---|
| Seed commitment | No journal or wallet mutation. An administrator stores only the SHA-256 digest of a secret seed while the series is PUBLISHED, before sales can open. The public catalog exposes the digest and commitment timestamp during sales. | One draw is attached to the series in COMMITTED state; committed catalog/prize configuration cannot be edited. |
| Sales closure | No journal or wallet mutation. The service locks the series and allocated tickets, requires zero active reservations, and snapshots eligible serials/prizes with a digest. | `OPEN -> CLOSED`; the draw becomes `SALES_CLOSED`. |
| Seed reveal and selection | No journal or wallet mutation. The reveal must hash to the prior commitment. SHA-256 rejection sampling removes each selected serial from the frozen population. | Unique winners and their selection transcript are written; `CLOSED -> DRAWN`. |
| Prize posting | For each winner, debit the platform clearing account and credit the winner's available wallet in a balanced journal linked to an immutable award/idempotency record. | The draw becomes `PRIZES_POSTED`; a retry cannot create another credit. |
| Result publication | No new prize journal. Durable local winner notices are added in the same transaction. | DRAWN -> RESULT_PUBLISHED; public result endpoints reveal only the seed, proof transcript and result digest, while the serial manifest is read through bounded pages. |

The published serial-only manifest makes the selection reproducible without revealing ticket owner identities. GET /results/{series_id}/candidates pages it by serial number (maximum 1,000 per response), avoiding unbounded result or idempotency payloads. This local implementation does not provide externally sourced randomness, independent timestamping/fairness certification, a funded prize-reserve check, a payout executor, or external notification delivery.

## Phase 7 marketing and revenue flow

Coupon rules and capacity are locked when an order is created. The order freezes subtotal, discount, coupon code, and net payable amount; an unpaid cancellation releases its reserved coupon. Settlement consumes the redemption and posts the 45% prize pool, 20% marketing, 10% operations, 5% emergency reserve, and 20% profit/growth allocation from the appropriate pending account. Integer rounding remainder goes to profit/growth. Delivery retries never allocate revenue again.

An active referral claim may qualify on the referred buyer's first settled order; an active cashback campaign may generate one reward for a qualifying settlement. An administrator can attribute one settled order to an active affiliate only with an evidence reference and cannot attribute the affiliate owner's own order. These records are `PENDING_REVIEW` and have no wallet or journal movement. Admins may void them with a reason. A later funding, approval, payout, and reversal policy is required before money can be credited to recipients. Existing prize posting still debits clearing rather than the new prize pool.

## Evidence boundary

Payment screenshots, UTRs, and receiver confirmation are recorded as claims/evidence only. They cannot mark an order paid. Normal settlement requires independently verified evidence plus receiver confirmation; an admin override requires a recorded reason, evidence reference, verification source, and audit log.

## Deployment boundaries

- Provider-specific capability certification, payment initiation, and payout execution. The generic signed callback adapter remains disabled until a provider contract and webhook secret are configured.
- Telegram invoice dispatch and webhook registration. This service creates signed local invoice instructions and handles authenticated inbound updates; a separately operated bot must call `sendInvoice` and `setWebhook`. Refunds and transaction-history polling are not implemented here.
- Automatic payment destination verification.
- External email, SMS, or push notification delivery. The P2P worker persists in-app notices through its transactional outbox; the Phase 5 generic payment outbox only requests ticket allocation.
- External refund payout, payout verification, and `REFUNDED` transition.
- Wallet credit from a buyer's direct P2P payment.

These require provider capabilities, funding/accounting policy, and operational approval before a real-money launch.
