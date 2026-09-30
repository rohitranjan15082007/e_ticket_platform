"""Provider-neutral payment-adapter contracts.

Adapters only build payment instructions or authenticate/normalise provider
evidence. They never change an order, allocate a ticket, or decide that an
external payment has settled. Those actions remain the responsibility of a
database-backed payment orchestration service.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Mapping, Protocol, runtime_checkable
from uuid import UUID


class PaymentAdapterError(Exception):
    """Base error for an adapter boundary failure."""


class PaymentAdapterConfigurationError(PaymentAdapterError):
    """Raised when a disabled or incomplete adapter is asked to do work."""


class PaymentEvidenceValidationError(PaymentAdapterError):
    """Raised when provider-supplied evidence cannot be trusted or normalised."""


class PaymentInitiationAvailability(StrEnum):
    """Availability of instructions, never the state of a payment."""

    READY = "READY"
    DISABLED = "DISABLED"
    UNSUPPORTED = "UNSUPPORTED"


def _require_positive_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def normalize_currency(currency: str, *, field_name: str = "currency") -> str:
    """Return a canonical three-character currency code without coercion."""

    if not isinstance(currency, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = currency.strip().upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise ValueError(f"{field_name} must be a three-letter ASCII currency code")
    return normalized


def normalize_reference(value: str, *, field_name: str, max_length: int = 128) -> str:
    """Validate a non-empty opaque reference without provider-specific rules."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return normalized


def merchant_reference_for_order(order_id: UUID) -> str:
    """Create a short, provider-neutral reference bound to one platform order."""

    if not isinstance(order_id, UUID):
        try:
            order_id = UUID(str(order_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("order_id must be a UUID") from exc
    return f"order-{order_id.hex}"


def instruction_snapshot(values: Mapping[str, object]) -> Mapping[str, object]:
    """Copy instruction data before returning it from an adapter.

    A normal dictionary is used intentionally: FastAPI's dataclass encoder
    deep-copies fields before serialising them, which is incompatible with a
    ``mappingproxy``. Values are deliberately limited by adapter
    implementations to JSON-compatible primitives and plain dictionaries/lists
    so the service can persist this independent snapshot later.
    """

    return deepcopy(dict(values))


@dataclass(frozen=True, slots=True)
class PaymentCreationRequest:
    """Frozen platform and provider price data supplied to an adapter.

    ``amount_paise`` and ``currency`` are the platform order snapshot. A
    provider can use a different integer unit (Telegram Stars, for example),
    in which case ``provider_amount`` and ``provider_currency`` must be set by
    the orchestration layer. The adapter never converts paise into a provider
    denomination on its own.
    """

    order_id: UUID
    customer_reference: str
    amount_paise: int
    currency: str = "INR"
    provider_amount: int | None = None
    provider_currency: str | None = None
    merchant_reference: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            order_id = self.order_id if isinstance(self.order_id, UUID) else UUID(str(self.order_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("order_id must be a UUID") from exc

        amount_paise = _require_positive_int(self.amount_paise, field_name="amount_paise")
        currency = normalize_currency(self.currency)
        customer_reference = normalize_reference(
            self.customer_reference, field_name="customer_reference"
        )
        provider_amount = self.provider_amount
        if provider_amount is not None:
            provider_amount = _require_positive_int(provider_amount, field_name="provider_amount")
        provider_currency = self.provider_currency
        if provider_currency is not None:
            provider_currency = normalize_currency(provider_currency, field_name="provider_currency")
        merchant_reference = self.merchant_reference or merchant_reference_for_order(order_id)
        merchant_reference = normalize_reference(
            merchant_reference, field_name="merchant_reference", max_length=128
        )

        object.__setattr__(self, "order_id", order_id)
        object.__setattr__(self, "amount_paise", amount_paise)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "customer_reference", customer_reference)
        object.__setattr__(self, "provider_amount", provider_amount)
        object.__setattr__(self, "provider_currency", provider_currency)
        object.__setattr__(self, "merchant_reference", merchant_reference)
        object.__setattr__(self, "metadata", instruction_snapshot(self.metadata))

    @property
    def resolved_provider_amount(self) -> int:
        """Provider amount, defaulting only to the same integer frozen amount."""

        return self.provider_amount if self.provider_amount is not None else self.amount_paise

    @property
    def resolved_provider_currency(self) -> str:
        """Provider currency, defaulting only to the frozen order currency."""

        return self.provider_currency if self.provider_currency is not None else self.currency


@dataclass(frozen=True, slots=True)
class PaymentCreation:
    """Local payment instructions; this is not a provider-payment result."""

    provider_namespace: str
    merchant_reference: str
    provider_amount: int
    provider_currency: str
    instructions: Mapping[str, object]
    availability: PaymentInitiationAvailability = PaymentInitiationAvailability.READY
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_namespace",
            normalize_reference(self.provider_namespace, field_name="provider_namespace", max_length=64),
        )
        object.__setattr__(
            self,
            "merchant_reference",
            normalize_reference(self.merchant_reference, field_name="merchant_reference", max_length=128),
        )
        object.__setattr__(
            self,
            "provider_amount",
            _require_positive_int(self.provider_amount, field_name="provider_amount"),
        )
        object.__setattr__(
            self,
            "provider_currency",
            normalize_currency(self.provider_currency, field_name="provider_currency"),
        )
        object.__setattr__(self, "instructions", instruction_snapshot(self.instructions))
        if self.availability is PaymentInitiationAvailability.READY and self.unavailable_reason:
            raise ValueError("READY payment instructions cannot have an unavailable_reason")
        if self.availability is not PaymentInitiationAvailability.READY and not self.unavailable_reason:
            raise ValueError("unavailable payment instructions require an unavailable_reason")

    @property
    def is_available(self) -> bool:
        return self.availability is PaymentInitiationAvailability.READY


@dataclass(frozen=True, slots=True)
class AuthenticatedWebhook:
    """Authentication result for raw provider data, without settlement meaning."""

    provider_namespace: str
    raw_body_sha256: str


@runtime_checkable
class PaymentAdapter(Protocol):
    """Instruction-only adapter interface consumed by payment orchestration."""

    provider_namespace: str

    def create_payment(self, request: PaymentCreationRequest) -> PaymentCreation:
        """Build instructions only; never report an external payment as settled."""


@runtime_checkable
class SignedWebhookAdapter(Protocol):
    """Optional capability for adapters that authenticate raw callback bodies."""

    provider_namespace: str

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str | None) -> None:
        """Raise when the callback is not authenticated; return no payment result."""


def authenticated_webhook(*, provider_namespace: str, raw_body: bytes) -> AuthenticatedWebhook:
    """Create a storage-friendly digest after an adapter authenticates a body."""

    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")
    return AuthenticatedWebhook(
        provider_namespace=provider_namespace,
        raw_body_sha256=sha256(raw_body).hexdigest(),
    )
