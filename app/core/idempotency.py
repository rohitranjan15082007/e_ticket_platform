"""Idempotency fingerprints and durable mutation replay records."""

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.transaction_locks import lock_key_for_transaction
from app.exceptions import ConflictError, ValidationError
from app.models.user import IdempotencyRecord


def _normalize(value: object) -> object:
    if isinstance(value, (float, Decimal)):
        raise ValidationError("INVALID_MONEY_TYPE", "floats and Decimal values are not allowed in mutations")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_normalize(item) for item in value]
    raise ValidationError("INVALID_IDEMPOTENCY_PAYLOAD", f"Unsupported payload type: {type(value).__name__}")


def canonical_payload(value: Mapping[str, object]) -> dict[str, object]:
    """Create a JSON-safe payload while rejecting non-deterministic money types."""

    normalized = _normalize(value)
    if not isinstance(normalized, dict):
        raise ValidationError("INVALID_IDEMPOTENCY_PAYLOAD", "payload must be an object")
    return normalized


def fingerprint(value: Mapping[str, object]) -> str:
    """Hash a canonical mutation payload for same-key replay protection."""

    encoded = json.dumps(canonical_payload(value), sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


class IdempotencyService:
    """Reads and creates records inside the caller's database transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_replay(
        self, *, actor_scope: str, key: str, request_fingerprint: str
    ) -> IdempotencyRecord | None:
        self._validate_scope(actor_scope)
        self._validate_key(key)
        # A row lock cannot protect the empty-result case.  PostgreSQL uses a
        # transaction-scoped advisory lock for this logical key, so two
        # concurrent requests cannot both decide to insert it.  SQLite keeps
        # the same flow for local coverage and serializes writers itself.
        await lock_key_for_transaction(self.session, "idempotency", actor_scope, key)
        record = await self.session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.actor_scope == actor_scope,
                IdempotencyRecord.idempotency_key == key,
            )
            .with_for_update()
        )
        if record is None:
            return None
        if record.request_fingerprint != request_fingerprint:
            raise ConflictError(
                "IDEMPOTENCY_CONFLICT", "Idempotency-Key was already used with different data"
            )
        return record

    def record(
        self,
        *,
        actor_scope: str,
        key: str,
        request_fingerprint: str,
        resource_type: str,
        resource_id: UUID,
        status_code: int = 200,
        response_payload: Mapping[str, object] | None = None,
        record_id: UUID | None = None,
    ) -> IdempotencyRecord:
        self._validate_scope(actor_scope)
        self._validate_key(key)
        record = IdempotencyRecord(
            **({"id": record_id} if record_id is not None else {}),
            actor_scope=actor_scope,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            resource_type=resource_type,
            resource_id=resource_id,
            status_code=status_code,
            response_payload=(canonical_payload(response_payload) if response_payload is not None else None),
        )
        self.session.add(record)
        return record

    @staticmethod
    def _validate_key(key: object) -> None:
        if not isinstance(key, str) or not 8 <= len(key) <= 255:
            raise ValidationError(
                "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be a string between 8 and 255 characters"
            )

    @staticmethod
    def _validate_scope(actor_scope: object) -> None:
        if not isinstance(actor_scope, str) or not 1 <= len(actor_scope) <= 400:
            raise ValidationError(
                "INVALID_IDEMPOTENCY_SCOPE",
                "Idempotency actor scope must be a string between 1 and 400 characters",
            )
