# Security and pre-launch gates

Status: **Phase 9 local implementation complete; PostgreSQL race/trigger tests
verified locally on 2026-09-26; not approved for real-money production**.

This checklist separates controls visible in this repository from reviews that require
independent evidence. A checked item means its local behavior has tests, not that a
deployment or provider has been certified.

## Locally verified controls

- [x] Production settings reject the shipped JWT secrets, debug mode, sample
  PostgreSQL credentials, non-PostgreSQL database URLs, wildcard CORS and non-HTTPS
  browser origins. Secret randomness and rotation still require operations.
- [x] Browser pages use a same-origin CSP. API and HTML responses carry no-store,
  no-sniff, no-frame and restrictive browser feature headers.
- [x] `/health` is liveness only; `/ready` probes PostgreSQL and Redis with bounded
  waits, returns 503 on dependency failure, and never sends exception/DSN text.
- [x] Read-only journal and wallet-cache integrity commands report anomalies
  with non-zero exit codes; SQLite tests cover balanced, held, and mismatched
  cases. A production PostgreSQL run and alert connection are still pending.
- [x] Financial mutations use server-side integer paise, idempotency, role checks,
  audit records and settlement-gated delivery. UTR/screenshot or receiver claim
  alone never settles an order.
- [x] Signed webhook and replay boundaries have local tests. These tests do not
  certify a particular live provider's signing behavior.
- [x] Administrator audit reads enforce persisted roles, bounded pagination, and
  metadata-only selection. Snapshot and reason fields are not loaded or returned.
- [x] Nine PostgreSQL locking, trigger and race tests passed against an isolated
  disposable PostgreSQL 16.15 test database on 2026-09-26. The full local suite
  reported 137 passed, zero skipped and one expected-failure readiness check.

## Required before live operation

- [ ] Independent threat model, penetration test, dependency review and remediation.
  Review authentication abuse/rate limiting, evidence uploads, session storage/XSS,
  CORS/CSP, admin operations, webhook replay and secret rotation.
- [ ] Legal/compliance owner signs off on lottery/ticket rules, jurisdiction, KYC/AML,
  privacy, tax, refund and payout policy. This repository cannot confer approval.
- [ ] Select and contract actual P2P, UPI, white-label and Telegram bot operators.
  Verify each provider's signing, reconciliation, refund and incident procedures
  with test credentials before enabling a method.
- [ ] Perform PostgreSQL load tests for inventory, withdrawals, idempotency,
  outbox and webhook bursts in an environment representative of deployment.
- [ ] Establish a real secret manager and rotation drill. Replace the development
  Compose database password; do not expose PostgreSQL or Redis directly to the
  internet. TLS termination, HSTS and network policy need deployment review.
- [ ] Set retention/access policies for identity, payment proofs, UTRs, audit trails
  and logs; ensure logs and monitoring do not contain credentials or raw evidence.
- [ ] Agree recovery objectives; schedule encrypted, access-controlled, offsite
  PostgreSQL backups and test a restore to an isolated environment. Reconcile
  wallet caches, journal groups, settlements, ticket allocations and outboxes
  after recovery. No backup or restore was executed by this project.
- [ ] Define SLOs and alerts for `/ready`, database/broker reachability, stale
  outbox items, failed tasks, pending manual reviews, unresolved P2P exposure,
  non-zero integrity-check exits and backup freshness. Exercise alert routing.
- [ ] Obtain an independent draw/fairness review, including commitment timing,
  candidate freeze, entropy custody and publication reproducibility.
- [ ] Browser-assisted accessibility and end-to-end buyer/admin testing on mobile
  and desktop; Phase 8's route/CSS checks are not a visual audit.

Production release remains blocked until each external gate has a named owner,
dated evidence, and an approved rollback/incident procedure.
