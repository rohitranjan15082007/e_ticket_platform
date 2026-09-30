"""Redacted operational audit-log response."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_user_id: UUID | None
    entity_type: str
    entity_id: UUID
    action: str
    created_at: datetime
