"""Strict signed-P2P-provider webhook contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


def _text(value: object, *, field: str, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str) or not value.strip() or not minimum <= len(value.strip()) <= maximum:
        raise ValueError(f"{field} must be a non-empty string between {minimum} and {maximum} characters")
    return value.strip()


class P2PProviderWebhookRequest(BaseModel):
    """Provider facts only; the client cannot supply a settlement decision."""

    model_config = ConfigDict(extra="forbid")

    provider_namespace: str
    external_event_id: str
    event_type: Literal["PAYMENT_VERIFIED", "PAYMENT_PENDING", "PAYMENT_REJECTED"]
    match_id: UUID
    payment_submission_id: UUID
    transaction_reference: str
    verified_amount_paise: int
    verified_currency: str = "INR"
    recipient_fingerprint: str
    occurred_at: datetime

    @field_validator("provider_namespace")
    @classmethod
    def validate_namespace(cls, value: object) -> str:
        return _text(value, field="provider_namespace", maximum=64)

    @field_validator("external_event_id")
    @classmethod
    def validate_external_id(cls, value: object) -> str:
        return _text(value, field="external_event_id", minimum=8, maximum=160)

    @field_validator("transaction_reference")
    @classmethod
    def validate_reference(cls, value: object) -> str:
        return _text(value, field="transaction_reference", maximum=160)

    @field_validator("verified_amount_paise", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("verified_amount_paise must be a positive integer paise value")
        return value

    @field_validator("verified_currency")
    @classmethod
    def validate_currency(cls, value: object) -> str:
        if not isinstance(value, str) or value.upper() != "INR":
            raise ValueError("verified_currency must be INR")
        return "INR"

    @field_validator("recipient_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: object) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in value)
        ):
            raise ValueError("recipient_fingerprint must be a SHA-256 hexadecimal digest")
        return value.lower()


class P2PProviderWebhookResponse(BaseModel):
    id: UUID
    status: str
    replayed: bool
    review_required: bool
    settlement_id: UUID | None = None
