"""P2P payment-attempt, proof, confirmation and admin-review contracts."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.order import DeliveryStatus, OrderStatus
from app.models.p2p_match import (
    AdminResolutionDecision,
    P2PMatchStatus,
    P2PRefundStatus,
    PaymentSubmissionVerificationStatus,
    ReceiverConfirmationDecision,
)
from app.schemas.order import OrderResponse
from app.schemas.withdrawal import _positive_paise


def _clean_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"{field} must be a non-empty string up to {maximum} characters")
    return value.strip()


class P2PMatchResponse(BaseModel):
    id: UUID
    order_id: UUID
    withdrawal_id: UUID
    buyer_user_id: UUID
    receiver_user_id: UUID
    amount_paise: int
    currency: str
    status: P2PMatchStatus
    destination_exposed: bool
    payment_deadline_at: datetime
    receiver_confirmation_deadline_at: datetime | None
    instructions_disabled_at: datetime | None
    instructions_exposed_at: datetime | None
    settled_at: datetime | None
    closed_at: datetime | None
    destination_snapshot: dict[str, object] | None


class P2PQueueResponse(BaseModel):
    order: OrderResponse
    match: P2PMatchResponse | None
    message: str
    closed_match: P2PMatchResponse | None = None


class PaymentSubmissionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_namespace: str
    claimed_reference: str
    observed_amount_paise: int
    observed_currency: str = "INR"
    declared_paid_at: datetime
    evidence_upload_id: UUID | None = None
    provider_evidence_reference: str | None = None
    evidence_metadata: dict[str, str] | None = None

    @field_validator("provider_namespace")
    @classmethod
    def validate_namespace(cls, value: object) -> str:
        return _clean_text(value, field="provider_namespace", maximum=64)

    @field_validator("claimed_reference")
    @classmethod
    def validate_reference(cls, value: object) -> str:
        return _clean_text(value, field="claimed_reference", maximum=160)

    @field_validator("observed_amount_paise", mode="before")
    @classmethod
    def validate_observed_amount(cls, value: object) -> int:
        return _positive_paise(value, field="observed_amount_paise")

    @field_validator("observed_currency")
    @classmethod
    def validate_currency(cls, value: object) -> str:
        if not isinstance(value, str) or value.upper() != "INR":
            raise ValueError("observed_currency must be INR")
        return "INR"

    @field_validator("provider_evidence_reference")
    @classmethod
    def validate_optional_evidence(cls, value: object) -> str | None:
        if value is None:
            return None
        return _clean_text(value, field="provider_evidence_reference", maximum=255)


class PaymentSubmissionResponse(BaseModel):
    id: UUID
    match_id: UUID
    buyer_user_id: UUID
    provider_namespace: str
    claimed_reference: str
    expected_amount_paise: int
    expected_currency: str
    observed_amount_paise: int
    observed_currency: str
    declared_paid_at: datetime
    evidence_upload_id: UUID | None
    provider_evidence_reference: str | None
    verification_status: PaymentSubmissionVerificationStatus
    verification_reason: str | None
    is_late: bool


class PaymentSubmissionMutationResponse(BaseModel):
    match: P2PMatchResponse
    submission: PaymentSubmissionResponse


class ReceiverConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ReceiverConfirmationDecision
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: object) -> str | None:
        if value is None:
            return None
        return _clean_text(value, field="reason", maximum=500)


class ReceiverConfirmationResponse(BaseModel):
    id: UUID
    match_id: UUID
    receiver_user_id: UUID
    decision: ReceiverConfirmationDecision
    reason: str | None
    confirmed_at: datetime


class ReceiverConfirmationMutationResponse(BaseModel):
    match: P2PMatchResponse
    confirmation: ReceiverConfirmationResponse


class AdminDisputeResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: AdminResolutionDecision
    payment_submission_id: UUID | None = None
    reason: str
    evidence_reference: str
    verification_source: str
    release_hold: bool = False

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: object) -> str:
        return _clean_text(value, field="reason", maximum=500)

    @field_validator("evidence_reference")
    @classmethod
    def validate_evidence(cls, value: object) -> str:
        return _clean_text(value, field="evidence_reference", maximum=255)

    @field_validator("verification_source")
    @classmethod
    def validate_source(cls, value: object) -> str:
        return _clean_text(value, field="verification_source", maximum=100)


class CloseUnpaidRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    evidence_reference: str
    release_hold: bool = False

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: object) -> str:
        return _clean_text(value, field="reason", maximum=500)

    @field_validator("evidence_reference")
    @classmethod
    def validate_evidence(cls, value: object) -> str:
        return _clean_text(value, field="evidence_reference", maximum=255)


class SettlementDetailsResponse(BaseModel):
    id: UUID
    journal_group_id: UUID
    amount_paise: int
    currency: str
    verification_source: str
    settled_at: datetime


class P2PSettlementResponse(BaseModel):
    match: P2PMatchResponse
    order: OrderResponse
    settlement: SettlementDetailsResponse


class DeliveryRetryResponse(BaseModel):
    id: UUID
    status: OrderStatus
    delivery_status: DeliveryStatus
    total_paise: int
    currency: str
    expires_at: datetime


class RefundCaseCreateRequest(BaseModel):
    """Admin evidence fields for a case; it contains no payout destination or status."""

    model_config = ConfigDict(extra="forbid")

    match_id: UUID
    amount_paise: int
    currency: str = "INR"
    reason: str
    liable_party: str
    funding_source_reference: str
    destination_validation_reference: str
    executor_reference: str

    @field_validator("amount_paise", mode="before")
    @classmethod
    def validate_amount(cls, value: object) -> int:
        return _positive_paise(value, field="amount_paise")

    @field_validator("currency")
    @classmethod
    def validate_refund_currency(cls, value: object) -> str:
        if not isinstance(value, str) or value.upper() != "INR":
            raise ValueError("currency must be INR")
        return "INR"

    @field_validator("reason")
    @classmethod
    def validate_refund_reason(cls, value: object) -> str:
        return _clean_text(value, field="reason", maximum=500)

    @field_validator("liable_party")
    @classmethod
    def validate_liable_party(cls, value: object) -> str:
        return _clean_text(value, field="liable_party", maximum=100)

    @field_validator(
        "funding_source_reference", "destination_validation_reference", "executor_reference"
    )
    @classmethod
    def validate_refund_references(cls, value: object) -> str:
        return _clean_text(value, field="refund reference", maximum=255)


class RefundCaseResponse(BaseModel):
    id: UUID
    match_id: UUID
    settlement_id: UUID | None
    verified_payment_reference_id: UUID
    amount_paise: int
    currency: str
    reason: str
    liable_party: str
    funding_source_reference: str
    destination_validation_reference: str
    executor_reference: str
    status: P2PRefundStatus
    payout_reference: str | None
    payout_verified_at: datetime | None
