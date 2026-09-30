# Project Rules

## Stack

- Python 3.11+
- FastAPI
- PostgreSQL
- SQLAlchemy 2.0 async
- Alembic
- Pydantic v2
- pytest
- Celery + Redis
- Docker Compose

## Money Rules (CRITICAL)

- NEVER use float for money
- ALWAYS integer paise (₹1 = 100 paise)
- DB columns: BIGINT
- Every journal group must balance

## Spec Rules

- Follow SPEC.md (v1.2) exactly
- 50% rule = `floor(B * 5000 / 10000)`
- Threshold override = DISABLED by default
- One unresolved withdrawal per user
- Exact amount matching only (V1)
- No auto-release after payment exposure
- Screenshot alone cannot settle
- Backend enforces all state transitions

## Code Rules

- No business logic in routes
- Money logic only in `app/services/`
- All mutations require idempotency key
- All state changes require audit log
- Use `SELECT FOR UPDATE` for wallet/match
- Every service function has a test

## Never Do

- No float / Decimal for money
- No fake provider verification
- No frontend-driven state changes
- No partial matching in V1
- No silent ledger edits (use reversals)

## Tests

- Every SPEC section 16 test must exist
- pytest with async support
- Concurrency tests with `asyncio.gather`
- Idempotency tests call twice
