"""Safe in-app notification list projection."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class NotificationReadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: str
    event_type: str
    occurred_at: datetime
