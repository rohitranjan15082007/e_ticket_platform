"""Strict API contracts for Phase 5 order-linked payment methods.

The platform's order price remains integer INR paise.  A provider can use a
different *provider* unit (Telegram Stars uses ``XTR``), so these contracts
never coerce a provider amount into paise or vice versa.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.payment import (
    ManualPaymentDestinationStatus,
    ManualPaymentProofStatus,
    PaymentAttemptStatus,
    PaymentMethod,
    PaymentProviderEventStatus,
)
from app.schemas.order import OrderResponse
from app.schemas.ticket_series import _strict_positive_int


MAX_MONEY = 9_000_000_000_000_000_000


def _text(value: object, *, field: str, maximum: int, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} must be a non-empty string up to {maximum} characters")
    return normalized


class PaymentStartRequest(BaseModel):
    """Start one configured payment path for an unexposed order."""

    model_config = ConfigDict(extra="forbid")

    method: PaymentMethod
    # The Telegram identity is explicit so an invoice cannot be paid by an
    # arbitrary bot account and credited to this buyer's order.
    telegram_user_id: int | None = None

    @field_validator("telegram_user_id", mode="before")
    @classmethod
    def validate_telegram_user_id(cls, value: object) -> int | None:
        if value is None:
            return None
        return _strict_positive_int(value, field="telegram_user_id", maximum=9_223_372_036_854_775_807)


class PaymentMethodAvailabilityResponse(BaseModel):
    """Display hints only; the payment service rechecks availability on start."""

    p2p_match: bool
    manual_upi: bool
    telegram_stars: bool
    white_label: bool = False


class PaymentAttemptResponse(BaseModel):
    id: UUID
    order_id: UUID
    buyer_user_id: UUID
    method: PaymentMethod
    provider_namespace: str
    status: PaymentAttemptStatus
    order_amount_paise: int
    order_currency: str
    provider_amount: int
    provider_currency: str
    merchant_reference: str
    provider_order_reference: str | None
    provider_payment_reference: str | None
    telegram_invoice_payload: str | None
    telegram_user_id: int | None
    manual_destination_id: UUID | None
    method_data_snapshot: dict[str, object]
    expires_at: datetime
    settled_at: datetime | None
    terminal_reason: str | None


class PaymentStartResponse(BaseModel):
    order: OrderResponse
    payment: PaymentAttemptResponse
    replayed: bool = False


class ManualPaymentDestinationCreateRequest(BaseModel):
    """An administrator-owned approved UPI/QR instruction source."""

    model_config = ConfigDict(extra="forbid")

    display_label: str
    upi_id: str
    # A UPI URI is sufficient for a manual method; deployments may attach a
    # QR asset, but should not have to invent one before an approved UPI
    # destination can be used.
    qr_reference: str | None = None
    instructions: dict[str, str] = Field(default_factory=dict)
    approval_evidence_reference: str

    @field_validator("display_label")
    @classmethod
    def validate_label(cls, value: object) -> str:
        return str(_text(value, field="display_label", maximum=120))

    @field_validator("upi_id")
    @classmethod
    def validate_upi_id(cls, value: object) -> str:
        return str(_text(value, field="upi_id", maximum=255))

    @field_validator("approval_evidence_reference")
    @classmethod
    def validate_reference(cls, value: object) -> str:
        return str(_text(value, field="reference", maximum=255))

    @field_validator("qr_reference")
    @classmethod
    def validate_qr_reference(cls, value: object) -> str | None:
        return _text(value, field="qr_reference", maximum=255, allow_none=True)

    @field_validator("instructions")
    @classmethod
    def validate_instructions(cls, value: object) -> dict[str, str]:
        if not isinstance(value, dict) or len(value) > 30:
            raise ValueError("instructions must be an object with at most 30 entries")
        result: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip() or len(key.strip()) > 100:
                raise ValueError("instruction keys must be non-empty strings up to 100 characters")
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > 1000:
                raise ValueError("instruction values must be non-empty strings up to 1000 characters")
            result[key.strip()] = item.strip()
        return result


class ManualPaymentDestinationResponse(BaseModel):
    id: UUID
    display_label: str
    upi_id: str
    qr_reference: str | None
    instructions: dict[str, object]
    status: ManualPaymentDestinationStatus
    approved_by_user_id: UUID | None
    approval_evidence_reference: str | None
    approved_at: datetime | None


class ManualPaymentProofCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    utr: str
    proof_reference: str
    proof_metadata: dict[str, str] | None = None
    submitted_amount_paise: int
    submitted_currency: str = "INR"

    @field_validator("utr")
    @classmethod
    def validate_utr(cls, value: object) -> str:
        return str(_text(value, field="utr", maximum=160))

    @field_validator("proof_reference")
    @classmethod
    def validate_proof_reference(cls, value: object) -> str:
        return str(_text(value, field="proof_reference", maximum=255))

    @field_validator("proof_metadata")
    @classmethod
    def validate_proof_metadata(cls, value: object) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or len(value) > 30:
            raise ValueError("proof_metadata must be an object with at most 30 entries")
        result: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str) or len(key) > 100 or len(item) > 1000:
                raise ValueError("proof_metadata keys and values must be bounded strings")
            result[key] = item
        return result

    @field_validator("submitted_amount_paise", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> int:
        return _strict_positive_int(value, field="submitted_amount_paise", maximum=MAX_MONEY)

    @field_validator("submitted_currency")
    @classmethod
    def validate_currency(cls, value: object) -> str:
        if not isinstance(value, str) or value.upper() != "INR":
            raise ValueError("submitted_currency must be INR")
        return "INR"


class ManualPaymentProofResponse(BaseModel):
    id: UUID
    payment_attempt_id: UUID
    manual_destination_id: UUID
    buyer_user_id: UUID
    utr: str
    proof_reference: str
    submitted_amount_paise: int
    submitted_currency: str
    status: ManualPaymentProofStatus
    reviewed_by_user_id: UUID | None
    reviewed_at: datetime | None
    review_note: str | None


class ManualProofMutationResponse(BaseModel):
    payment: PaymentAttemptResponse
    proof: ManualPaymentProofResponse | None
    review_required: bool
    replayed: bool = False


class ManualReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    KEEP_IN_REVIEW = "KEEP_IN_REVIEW"


class ManualPaymentReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ManualReviewDecision
    review_note: str

    @field_validator("review_note")
    @classmethod
    def validate_note(cls, value: object) -> str:
        return str(_text(value, field="review_note", maximum=500))


class ManualPaymentReviewResponse(BaseModel):
    payment: PaymentAttemptResponse
    proof: ManualPaymentProofResponse
    settlement_id: UUID | None
    review_required: bool
    replayed: bool = False


class WhiteLabelWebhookRequest(BaseModel):
    """Provider-neutral signed callback contract until a provider is selected."""

    model_config = ConfigDict(extra="forbid")

    provider_namespace: str
    external_event_id: str
    event_type: str
    merchant_reference: str
    provider_payment_reference: str | None = None
    provider_amount: int
    provider_currency: str = "INR"
    occurred_at: datetime | None = None

    @field_validator("provider_namespace")
    @classmethod
    def validate_namespace(cls, value: object) -> str:
        return str(_text(value, field="provider_namespace", maximum=64))

    @field_validator("external_event_id", "merchant_reference")
    @classmethod
    def validate_event_text(cls, value: object) -> str:
        return str(_text(value, field="provider event field", maximum=160))

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: object) -> str:
        result = str(_text(value, field="event_type", maximum=64)).upper()
        if result not in {"PAYMENT_SUCCEEDED", "PAYMENT_PENDING", "PAYMENT_FAILED"}:
            raise ValueError("event_type must be PAYMENT_SUCCEEDED, PAYMENT_PENDING, or PAYMENT_FAILED")
        return result

    @field_validator("provider_payment_reference")
    @classmethod
    def validate_provider_payment_reference(cls, value: object) -> str | None:
        return _text(value, field="provider_payment_reference", maximum=160, allow_none=True)

    @field_validator("provider_amount", mode="before")
    @classmethod
    def validate_provider_amount(cls, value: object) -> int:
        return _strict_positive_int(value, field="provider_amount", maximum=MAX_MONEY)

    @field_validator("provider_currency")
    @classmethod
    def validate_provider_currency(cls, value: object) -> str:
        if not isinstance(value, str) or value.upper() != "INR":
            raise ValueError("provider_currency must be INR for the generic white-label boundary")
        return "INR"


class PaymentWebhookResponse(BaseModel):
    id: UUID
    status: PaymentProviderEventStatus
    payment_attempt_id: UUID | None
    settlement_id: UUID | None
    review_required: bool
    replayed: bool = False


class TelegramUser(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: int

    @field_validator("id", mode="before")
    @classmethod
    def validate_id(cls, value: object) -> int:
        return _strict_positive_int(value, field="telegram user id", maximum=9_223_372_036_854_775_807)


class TelegramPreCheckoutQuery(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    from_user: TelegramUser = Field(alias="from")
    currency: str
    total_amount: int
    invoice_payload: str

    @field_validator("id", "invoice_payload")
    @classmethod
    def validate_text(cls, value: object) -> str:
        return str(_text(value, field="Telegram pre-checkout field", maximum=255))

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: object) -> str:
        if not isinstance(value, str) or value.upper() != "XTR":
            raise ValueError("Telegram Stars currency must be XTR")
        return "XTR"

    @field_validator("total_amount", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> int:
        return _strict_positive_int(value, field="Telegram Stars total_amount", maximum=MAX_MONEY)


class TelegramSuccessfulPayment(BaseModel):
    model_config = ConfigDict(extra="allow")

    currency: str
    total_amount: int
    invoice_payload: str
    telegram_payment_charge_id: str

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: object) -> str:
        if not isinstance(value, str) or value.upper() != "XTR":
            raise ValueError("Telegram Stars currency must be XTR")
        return "XTR"

    @field_validator("total_amount", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> int:
        return _strict_positive_int(value, field="Telegram Stars total_amount", maximum=MAX_MONEY)

    @field_validator("invoice_payload", "telegram_payment_charge_id")
    @classmethod
    def validate_text(cls, value: object) -> str:
        return str(_text(value, field="Telegram successful-payment field", maximum=255))


class TelegramMessage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    from_user: TelegramUser = Field(alias="from")
    successful_payment: TelegramSuccessfulPayment | None = None


class TelegramUpdate(BaseModel):
    """Subset of a Bot API update used by the inbound trust boundary."""

    model_config = ConfigDict(extra="allow")

    update_id: int
    pre_checkout_query: TelegramPreCheckoutQuery | None = None
    message: TelegramMessage | None = None

    @field_validator("update_id", mode="before")
    @classmethod
    def validate_update_id(cls, value: object) -> int:
        return _strict_positive_int(value, field="Telegram update_id", maximum=9_223_372_036_854_775_807)


class TelegramWebhookResponse(BaseModel):
    id: UUID
    update_id: int
    status: PaymentProviderEventStatus
    payment_attempt_id: UUID | None
    settlement_id: UUID | None
    review_required: bool
    replayed: bool = False
    pre_checkout_query_id: str | None = None
    pre_checkout_approved: bool | None = None
    pre_checkout_error: str | None = None
