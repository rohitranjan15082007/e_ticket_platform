# API contract

All endpoints below are under `/api/v1`, enforce backend ownership/role checks, and require an `Idempotency-Key` header for user/admin mutations. The three authenticated provider-webhook routes are the exception: they are deduplicated by durable provider event/update IDs plus raw-payload digests rather than a caller-supplied idempotency header.

## Account and operations reads

| Method and path | Actor | Responsibility |
|---|---|---|
| `GET /profile` | Signed-in user | Read only that user's identity, verification flag and creation timestamp; no credential hash. |
| `GET /notifications?limit=50` | Signed-in user | Bounded delivered P2P/draw event metadata from existing durable notice tables; no payload or other user's records. |
| `GET /admin/users`, `GET /admin/users/{id}` | Admin | Bounded list and masked-email detail; no role mutation. |
| `GET /admin/ticket-packages`, `GET /admin/ticket-packages/{id}` | Admin | Read active and inactive package snapshots. |

## P2P receiver and buyer endpoints

| Method and path | Actor | Responsibility |
|---|---|---|
| `POST /withdrawals` | Receiver | Validate integer paise, snapshot eligibility, and create one named hold using a pre-verified destination. |
| `GET /withdrawals/{id}` | Owner or admin | Read the withdrawal state and its immutable snapshots. |
| `POST /withdrawals/{id}/cancel` | Receiver | Release only an unmatched, unexposed hold. |
| `POST /orders/{id}/p2p/match` | Order buyer | Queue or atomically claim the oldest exact withdrawal. |
| `GET /matches/{id}` | Assigned buyer/receiver or admin | Read the authorized frozen instruction snapshot and state. |
| `POST /matches/{id}/payment-submissions` | Assigned buyer | Store a UTR/proof claim; no claim is verification. |
| `POST /matches/{id}/receiver-confirmation` | Assigned receiver | Store received/not-received decision; received alone never settles. |

## Signed provider callback

| Method and path | Actor | Responsibility |
|---|---|---|
| `POST /payments/provider-webhook` | Configured provider | Accept a raw-body HMAC in `X-P2P-Signature`, persist the external event/replay digest, and apply only exact verified facts. It returns `202`; ambiguous, late, mismatched, or reused references remain in durable review. |

This endpoint is intentionally unauthenticated at the user layer. It is disabled until `TICKET_P2P_PROVIDER_WEBHOOK_SECRET` is configured, and it never accepts a buyer-supplied settlement decision.

## Phase 5 order payments

| Method and path | Actor | Responsibility |
|---|---|---|
| `POST /orders/{id}/payments` | Order buyer | Expose one configured manual UPI or Telegram Stars instruction path for an otherwise unexposed order. The server freezes all amount/unit/instruction facts and requires an idempotency key. |
| `GET /payments/{id}` | Attempt buyer | Read the buyer's own frozen payment attempt. |
| `POST /payments/{id}/manual-proof` | Attempt buyer | Store a UTR/proof reference and move it to review; it never declares payment success. A proof submitted after expiry is retained as late evidence for explicit administrator review, regardless of whether the expiry worker ran first. |

Starting a white-label payment currently returns `PAYMENT_METHOD_UNAVAILABLE`: the generic adapter intentionally has no approved provider checkout contract. It does not manufacture a URL or treat a callback secret as a provider integration. Telegram initiation returns a signed local invoice-instruction payload; this service does not call Telegram `sendInvoice` or register a webhook, so a separately operated bot must own those outbound operations.

## Phase 5 webhook boundaries

| Method and path | Actor | Responsibility |
|---|---|---|
| `POST /webhooks/white-label/{provider_namespace}` | Configured concrete provider | Verify an exact raw-body HMAC from `X-White-Label-Signature`, retain the event once, and settle only a matching active white-label attempt with the exact frozen INR amount/currency and a nonblank unique provider payment reference. Bad, late, incomplete, or conflicting evidence is retained for review. |
| `POST /webhooks/telegram` | Telegram Bot API | Verify `X-Telegram-Bot-Api-Secret-Token`, retain update ID/digest once, validate a pre-checkout query, and settle only a matching `successful_payment` with a unique Telegram charge ID. |

The Telegram secret stays in a header rather than a URL path. A pre-checkout acknowledgement is an invoice-validation response, not payment settlement; the route bounds its end-to-end acknowledgement work to nine seconds and returns a retryable `503` if it cannot relay the acknowledgement after durable update capture. The configured webhook URL must be an absolute HTTPS URL, but this service deliberately does not invoke Telegram `setWebhook`, `sendInvoice`, `refundStarPayment`, or `getStarTransactions`.

## Phase 5 admin manual UPI review

| Method and path | Responsibility |
|---|---|
| `POST /admin/manual-upi/destinations` | Create the one active, administrator-approved UPI/QR destination; replacing it disables the previous destination while prior attempts retain their snapshots. |
| `GET /admin/manual-upi/review` | List submitted/under-review manual payment evidence. |
| `POST /admin/manual-upi/{attempt_id}/review` | Record `APPROVE`, `REJECT`, or `KEEP_IN_REVIEW` with an audit note. Only an exact-amount approved proof creates a settlement. |

## Phase 6 draw and published results

| Method and path | Actor | Responsibility |
|---|---|---|
| POST /admin/ticket-series/{id}/draw/commit | Admin | Store a lowercase SHA-256 commitment while the series is PUBLISHED, before sales can open. The raw seed is not persisted at this stage. |
| POST /admin/ticket-series/{id}/open | Admin | Open sales only when the published series has its immutable COMMITTED draw evidence. |
| `POST /admin/ticket-series/{id}/close` | Admin or guarded scheduler | At/after the sale end, atomically require zero reservations, freeze only `ALLOCATED` ticket serials and the prize snapshot, and move the series to `CLOSED`. A prior commitment is mandatory. |
| `POST /admin/ticket-series/{id}/draw/run` | Admin | Accept only a matching seed reveal at/after `draw_at`, select unique winners from the frozen snapshot with `sha256-rejection-sampling-v1`, and mark the series `DRAWN`. It accepts no ticket-selection input. |
| `POST /admin/ticket-series/{id}/draw/post-prizes` | Admin | Create one balanced, idempotent wallet credit and immutable award record for each winner. |
| `POST /admin/ticket-series/{id}/draw/publish` | Admin | Require every award, publish the result, and create durable local winner notices. |
| `GET /admin/ticket-series/{id}/draw` | Admin | Read operational draw state without exposing an unreleased seed. |
| GET /catalog/series and GET /catalog/series/{id} | Public | Read sellable series with the pre-sale seed commitment and commitment timestamp, never the raw seed. |
| GET /results and GET /results/{id} | Public | Read only RESULT_PUBLISHED evidence: seed reveal, digest, prize snapshot, and public winner proof. Owner identities and ledger IDs are omitted. |
| GET /results/{id}/candidates | Public | Read a bounded serial-only candidate-manifest page (after_serial_number, maximum limit=1000) to independently reproduce the candidate digest. |

Every Phase 6 mutation requires Idempotency-Key, persisted administrator authorization, row locks, and an audit record. Seed reveals accept only 16-512 visible ASCII characters so PostgreSQL cannot reject control bytes after request validation. The background close task does not perform a reveal, draw, prize credit, or publication.

## Phase 7 marketing and revenue

| Method and path | Actor | Responsibility |
|---|---|---|
| `POST /orders` with optional `coupon_code` | Buyer | Freeze gross, discount, and payable net price before payment; existing order cancellation releases an unused coupon. |
| `POST/GET /admin/marketing/coupons`, `POST /admin/marketing/coupons/{id}/deactivate` | Admin | Configure, list, or deactivate coupons with capacity limits and frozen redemption rules. |
| `POST/GET /admin/marketing/referral-programs`, `POST /admin/marketing/referral-programs/{id}/activate` or `/disable` | Admin | Configure and rotate one active fixed-reward referral program. |
| `POST/GET /referrals/profile`, `POST/GET /referrals/claim` | User | Obtain a referral code or claim another user's code before a first settlement. |
| `GET /admin/marketing/referral-rewards`, `POST /admin/marketing/referral-rewards/{id}/void` | Admin | Review and void non-wallet pending referral rewards. |
| `POST/GET /admin/marketing/cashback-campaigns`, `POST /admin/marketing/cashback-campaigns/{id}/activate` or `/disable` | Admin | Configure and rotate one active cashback campaign. |
| `GET /admin/marketing/cashback-rewards`, `POST /admin/marketing/cashback-rewards/{id}/void` | Admin | Review and void non-wallet pending cashback rewards. |
| `POST/GET /admin/marketing/affiliates`, `POST /admin/marketing/affiliates/{id}/activate` or `/suspend` | Admin | Configure and review affiliate accounts. |
| `POST/GET /admin/marketing/affiliate-conversions`, `POST /admin/marketing/affiliate-conversions/{id}/void`, `GET /admin/marketing/affiliate-commissions` | Admin | Attribute a settled order with evidence, review one pending commission, or void both linked records. |
| `GET /admin/revenue/report`, `GET /admin/revenue/allocations` | Admin | Read settled, immutable integer-paise revenue allocation totals and bounded records. |

All Phase 7 marketing mutations require `Idempotency-Key`, persisted role checks, and audit events. Reward records cannot be paid through these endpoints; provider, funding, approval, and reversal policies are still required.

## Phase 8 frontend support

All paths in this table have the `/api/v1` prefix. The same-origin HTML pages and assets are served outside that prefix.

| Method and path | Actor | Responsibility |
|---|---|---|
| `GET /wallet`, `GET /wallet/transactions` | Owner | Read available/held balances and bounded owner-only ledger entries. |
| `GET /orders/{id}/checkout-state` | Buyer | Recover the latest payment-attempt or P2P match navigation ID for an owned order without asserting settlement. |
| `POST /wallet/provision` | Owner | Idempotently create an empty INR wallet; no funds are added. |
| `GET /withdrawals`, `GET /withdrawals/destinations` | Owner | Read own bounded requests and already verified destination summaries. |
| `GET /admin/dashboard/summary` | Admin | Read operational counts and allocated-revenue total. |
| `GET /admin/ticket-series`, `GET /admin/ticket-series/{id}` | Admin | Read bounded series list or one series snapshot. |
| `GET /admin/withdrawals`, `GET /admin/withdrawals/{id}` | Admin | Read bounded withdrawal queue or one request. |
| `GET /admin/disputes` | Admin | Read bounded disputes with optional status filter. |
| `GET /admin/audit` | Admin | Read audit metadata only, newest first; `limit` (1-200, default 50), `offset` (0-1,000,000), and optional `actor_user_id`, `entity_id`, `entity_type`, `action` filters. Snapshot and reason fields are never returned. |

The browser never updates financial state locally. Manual UPI and P2P forms submit the amount actually paid as a claim; they cannot confirm settlement. Phase 8's administrator queue screens are read-only.

## Other admin endpoints

| Method and path | Responsibility |
|---|---|
| `POST /admin/disputes/{id}/resolve` | Evidence-required settle, close-unpaid, or keep-in-review decision. |
| `POST /admin/matches/{id}/close-unpaid` | Reconciled closure; explicitly retain for rematch or release/cancel the original hold. |
| `POST /admin/orders/{id}/retry-delivery` | P2P-only retry of entitlement allocation after payment without a second settlement. Generic Phase 5 delivery retry is handled by its own payment outbox worker. |
| `POST /admin/refunds` | Create one post-settlement `PENDING_EVIDENCE` refund case. It accepts no payout destination, payout reference, or target status. |

## Intentionally absent endpoints

- No endpoint treats a buyer claim as verified payment or accepts an arbitrary order status.
- No user endpoint can create a verified payment destination.
- No endpoint executes or verifies a refund payout in Phase 4.
- No endpoint accepts a screenshot, UTR, pre-checkout acknowledgement, manual order status, or unsigned provider payload as payment success.
- No Phase 5 endpoint triggers Telegram invoice dispatch, webhook registration, refund, or transaction-history polling; generic reconciliation creates review-only snapshots and never auto-settles a payment.
- No Phase 6 endpoint accepts a preferred winner/ticket ID or exposes a seed before result publication.

Typical domain errors include `INSUFFICIENT_ELIGIBLE_BALANCE`, `ACTIVE_WITHDRAWAL_EXISTS`, `INVALID_TRANSITION`, `REFERENCE_CONFLICT`, `IDEMPOTENCY_CONFLICT`, and `REVIEW_REQUIRED`.
