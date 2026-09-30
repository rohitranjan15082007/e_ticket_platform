"""Common read model over the existing durable P2P and draw notice tables.

P2PNotification and DrawNotification remain separate ORM persistence models.
This projection deliberately has no payload field, so list APIs cannot leak
provider evidence, internal draw data, or other users' notifications.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.models.p2p_match import P2PNotification
from app.models.winner import DrawNotification


@dataclass(frozen=True, slots=True)
class InAppNotification:
    id: UUID
    user_id: UUID
    source: Literal["P2P", "DRAW"]
    event_type: str
    occurred_at: datetime

    @classmethod
    def from_p2p(cls, notice: P2PNotification) -> "InAppNotification":
        return cls(
            id=notice.id, user_id=notice.user_id, source="P2P",
            event_type=notice.event_type, occurred_at=notice.delivered_at,
        )

    @classmethod
    def from_draw(cls, notice: DrawNotification) -> "InAppNotification":
        if notice.delivered_at is None:
            raise ValueError("Only delivered draw notices may be projected")
        return cls(
            id=notice.id, user_id=notice.user_id, source="DRAW",
            event_type=notice.event_type, occurred_at=notice.delivered_at,
        )
