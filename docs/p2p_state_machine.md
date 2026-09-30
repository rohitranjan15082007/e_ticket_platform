# P2P state machine

This document describes the implemented Phase 4 state boundaries. `SPEC.md` remains the authoritative product specification.

## Separate state records

| Record | Key states | Meaning |
|---|---|---|
| Withdrawal | `WAITING_FOR_BUYER`, `MATCHED`, `UNDER_REVIEW`, `COMPLETED`, `CANCELLED` | Receiver's named wallet hold and its matching lifecycle. |
| P2P match | `WAITING_FOR_PAYMENT`, `PAYMENT_SUBMITTED`, `WAITING_FOR_RECEIVER_CONFIRMATION`, `UNDER_VERIFICATION`, `EXPIRED_AWAITING_RECONCILIATION`, `DISPUTED`, `ADMIN_REVIEW`, `SETTLED`, `CLOSED_UNPAID`, `REFUND_PENDING`, `REFUNDED` | One immutable payment-instruction exposure for one exact order/withdrawal pair. |
| Provider event | `RECEIVED`, `PROCESSING`, `PROCESSED`, `REVIEW_REQUIRED`, `FAILED` | A raw-body HMAC-authenticated callback retained before it can affect any payment claim. |
| Notification outbox / notice | `PENDING`, `PROCESSING`, `DELIVERED`, `FAILED` / durable in-app notice | Transactional post-commit work; notification failure never reverses settlement. |
| Order | `PENDING_PAYMENT`, `WAITING_FOR_MATCH`, `AWAITING_PAYMENT`, `PAID`, `FULFILLED`, `REFUND_PENDING`, `REFUNDED` | Product reservation and delivery state, not proof verification state. |
| Delivery | `NOT_STARTED`, `PENDING`, `PROCESSING`, `DELIVERED`, `FAILED` | Entitlement processing after a durable settlement. |

## Normal path

1. Receiver creates a withdrawal: the platform snapshots pre-hold eligibility and moves wallet available funds into one named hold.
2. Buyer queues an eligible order. The oldest exact withdrawal is locked and claimed; the destination snapshot is frozen and the match moves to `WAITING_FOR_PAYMENT`.
3. Buyer submits a reference/proof: it is an `UNVERIFIED` claim, then the match waits for receiver confirmation or enters review.
4. Receiver's `RECEIVED` confirmation moves the match to `UNDER_VERIFICATION`; it does not settle it.
5. A configured signed provider callback is retained first, then must match the immutable recipient, submitted namespace/reference, exact INR amount, and receiver confirmation. Otherwise it remains in review. An evidence-required admin override is the other settlement path.
6. Settlement changes the match and withdrawal to `SETTLED`/`COMPLETED`, the order to `PAID`, and delivery to `PENDING`; it enqueues delivery and notification work atomically.
7. The P2P outbox worker allocates the entitlement separately and stores in-app notices. A failure leaves the payment settled and delivery `FAILED`; retry never posts another debit.

## Safe exception paths

- Payment deadline: `WAITING_FOR_PAYMENT` becomes `EXPIRED_AWAITING_RECONCILIATION`. Instructions are disabled, but the hold remains locked.
- Receiver timeout: `WAITING_FOR_RECEIVER_CONFIRMATION` becomes `UNDER_VERIFICATION`; it never becomes paid automatically.
- Receiver denial, wrong amount, late claim, or cross-match reference conflict enters controlled review/dispute states.
- Only an evidenced admin `CLOSED_UNPAID` decision can return an unexposed order/withdrawal to matching or release the hold while cancelling the order.
- A post-settlement refund case moves the match/order to `REFUND_PENDING` and stays `PENDING_EVIDENCE`. Phase 4 has no payout executor and no route to mark it paid/refunded.

## Invariants

- Amount and currency match exactly in integer INR paise; split matching is absent.
- One user has at most one unresolved withdrawal, and a partial unique index allows one active match per order and withdrawal.
- The recipient snapshot never changes after match creation.
- Each hold can be released or settled once; each match, withdrawal, and order can have at most one settlement.
- Wallet/match mutations are idempotent and audited. Related rows are locked in a consistent order: order, withdrawal, wallet, then match where applicable. A PostgreSQL transaction-scoped advisory lock serializes missing payment-reference and provider-event keys.
- A screenshot, UTR, or receiver confirmation alone cannot cross the settlement boundary.
