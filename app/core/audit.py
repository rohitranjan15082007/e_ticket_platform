"""Immutable audit logging for state-changing service operations."""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import canonical_payload
from app.models.audit_log import AuditLog


class AuditService:
    """Adds an audit event to the current transaction without committing it."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def record(
        self,
        *,
        actor_user_id: UUID | None,
        entity_type: str,
        entity_id: UUID,
        action: str,
        before_state: Mapping[str, object] | None,
        after_state: Mapping[str, object] | None,
        reason: str | None = None,
    ) -> AuditLog:
        event = AuditLog(
            actor_user_id=actor_user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            before_state=canonical_payload(before_state) if before_state is not None else None,
            after_state=canonical_payload(after_state) if after_state is not None else None,
            reason=reason,
        )
        self.session.add(event)
        return event
