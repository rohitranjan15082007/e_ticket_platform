"""Strict public contracts for P2P withdrawal requests."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.withdrawal import WithdrawalStatus


def _positive_paise(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer paise value")
    if value > 9_000_000_000_000_000:
        raise ValueError(f"{field} is too large")
    return value


class WithdrawalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_paise: int
    payment_destination_id: UUID

    @field_validator("amount_paise", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> int:
        return _positive_paise(value, field="amount_paise")


class WithdrawalResponse(BaseModel):
    id: UUID
    user_id: UUID
    wallet_id: UUID
    payment_destination_id: UUID
    wallet_hold_id: UUID | None
    amount_paise: int
    currency: str
    eligible_balance_snapshot_paise: int
    max_amount_snapshot_paise: int
    rule_snapshot: dict[str, object]
    rule_version: str
    status: WithdrawalStatus
    matched_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None


class VerifiedDestinationResponse(BaseModel):
    id: UUID
    provider_namespace: str
    display_label: str
    verified_at: datetime
