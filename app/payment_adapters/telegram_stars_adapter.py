"""Telegram Stars instruction, callback-validation, and pre-checkout boundary.

The adapter has no import-time network behaviour and does not regard a Bot API
response, pre-checkout acknowledgement, or ``successful_payment`` payload as a
platform settlement. It only creates/verifies evidence for the orchestration
service to persist, deduplicate, audit, and settle exactly once.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hmac
from hashlib import sha256
import json
from typing import Mapping
import urllib.error
import urllib.request
from uuid import UUID

from app.payment_adapters.base import (
    PaymentAdapterConfigurationError,
    PaymentAdapterError,
    PaymentCreation,
    PaymentCreationRequest,
    PaymentEvidenceValidationError,
    PaymentInitiationAvailability,
    instruction_snapshot,
    normalize_currency,
    normalize_reference,
)


TELEGRAM_STARS_CURRENCY = "XTR"
_INVOICE_PAYLOAD_VERSION = "ts1"
_BASE36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str, *, field_name: str) -> bytes:
    if not value or not value.isascii():
        raise PaymentEvidenceValidationError(f"Invalid {field_name}")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise PaymentEvidenceValidationError(f"Invalid {field_name}") from exc
    if _base64url_encode(decoded) != value:
        raise PaymentEvidenceValidationError(f"Invalid {field_name}")
    return decoded


def _base36_encode(value: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("value must be a positive integer")
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(_BASE36_ALPHABET[remainder])
    return "".join(reversed(digits))


def _base36_decode(value: str, *, field_name: str) -> int:
    if not value or any(character not in _BASE36_ALPHABET for character in value):
        raise PaymentEvidenceValidationError(f"Invalid {field_name}")
    decoded = int(value, 36)
    if decoded <= 0 or _base36_encode(decoded) != value:
        raise PaymentEvidenceValidationError(f"Invalid {field_name}")
    return decoded


def _telegram_user_id(value: int | str, *, field_name: str = "telegram_user_id") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive Telegram user id")
    try:
        normalized = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive Telegram user id") from exc
    if normalized <= 0 or str(normalized) != str(value).strip():
        raise ValueError(f"{field_name} must be a canonical positive Telegram user id")
    return normalized


@dataclass(frozen=True, slots=True)
class TelegramStarsConfiguration:
    """Explicit configuration; every default is disabled and network-free."""

    bot_token: str | None = None
    webhook_secret: str | None = None
    invoice_payload_secret: str | None = None
    allow_bot_api_calls: bool = False
    bot_api_base_url: str = "https://api.telegram.org"

    def __post_init__(self) -> None:
        for field_name in ("bot_token", "webhook_secret", "invoice_payload_secret"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    normalize_reference(value, field_name=field_name, max_length=512),
                )
        if not isinstance(self.allow_bot_api_calls, bool):
            raise ValueError("allow_bot_api_calls must be a boolean")
        if not isinstance(self.bot_api_base_url, str) or not self.bot_api_base_url.startswith("https://"):
            raise ValueError("bot_api_base_url must be an HTTPS URL")
        object.__setattr__(self, "bot_api_base_url", self.bot_api_base_url.rstrip("/"))

    @property
    def can_build_invoices(self) -> bool:
        return self.bot_token is not None and self.invoice_payload_secret is not None

    @property
    def can_authenticate_webhooks(self) -> bool:
        return self.webhook_secret is not None

    @property
    def is_configured(self) -> bool:
        return self.can_build_invoices and self.can_authenticate_webhooks

    @property
    def can_call_bot_api(self) -> bool:
        return self.allow_bot_api_calls and self.bot_token is not None


@dataclass(frozen=True, slots=True)
class TelegramInvoicePayload:
    """Decoded, HMAC-authenticated payload facts; not a payment outcome."""

    order_id: UUID
    telegram_user_id: int
    stars_amount: int
    raw_payload: str


@dataclass(frozen=True, slots=True)
class TelegramStarsPaymentEvidence:
    """Validated provider assertion that still requires durable service checks."""

    merchant_reference: str
    telegram_payment_charge_id: str
    order_id: UUID
    telegram_user_id: int
    stars_amount: int
    currency: str
    invoice_payload: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "merchant_reference",
            normalize_reference(self.merchant_reference, field_name="merchant_reference"),
        )
        object.__setattr__(
            self,
            "telegram_payment_charge_id",
            normalize_reference(
                self.telegram_payment_charge_id,
                field_name="telegram_payment_charge_id",
                max_length=256,
            ),
        )
        object.__setattr__(self, "telegram_user_id", _telegram_user_id(self.telegram_user_id))
        if isinstance(self.stars_amount, bool) or not isinstance(self.stars_amount, int) or self.stars_amount <= 0:
            raise ValueError("stars_amount must be a positive integer")
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        if self.currency != TELEGRAM_STARS_CURRENCY:
            raise ValueError("Telegram Stars evidence currency must be XTR")


@dataclass(frozen=True, slots=True)
class TelegramBotApiResult:
    """Result of an explicit Bot API call, with no settlement interpretation."""

    method: str
    response: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class TelegramStarsAdapter:
    """Safe Telegram Stars adapter that keeps provider facts separate from settlement."""

    configuration: TelegramStarsConfiguration = TelegramStarsConfiguration()
    provider_namespace: str = "telegram_stars"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_namespace",
            normalize_reference(self.provider_namespace, field_name="provider_namespace", max_length=64),
        )

    @property
    def is_configured(self) -> bool:
        return self.configuration.is_configured

    @property
    def supports_refunds(self) -> bool:
        return False

    def _payload_secret(self) -> bytes:
        if not self.configuration.invoice_payload_secret:
            raise PaymentAdapterConfigurationError(
                "Telegram invoice payload generation is disabled because no payload secret is configured."
            )
        return self.configuration.invoice_payload_secret.encode("utf-8")

    def build_invoice_payload(
        self,
        *,
        order_id: UUID,
        telegram_user_id: int | str,
        stars_amount: int,
    ) -> str:
        """Build an exact HMAC payload bound to frozen order, user, and Stars price."""

        try:
            order_id = order_id if isinstance(order_id, UUID) else UUID(str(order_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("order_id must be a UUID") from exc
        user_id = _telegram_user_id(telegram_user_id)
        if isinstance(stars_amount, bool) or not isinstance(stars_amount, int) or stars_amount <= 0:
            raise ValueError("stars_amount must be a positive integer")

        unsigned_payload = ".".join(
            (
                _INVOICE_PAYLOAD_VERSION,
                _base64url_encode(order_id.bytes),
                _base36_encode(user_id),
                _base36_encode(stars_amount),
            )
        )
        signature = _base64url_encode(
            hmac.new(self._payload_secret(), unsigned_payload.encode("ascii"), sha256).digest()
        )
        payload = f"{unsigned_payload}.{signature}"
        payload_size = len(payload.encode("utf-8"))
        if not 1 <= payload_size <= 128:
            raise PaymentAdapterError("Telegram invoice payload must be between 1 and 128 bytes")
        return payload

    def _parse_invoice_payload(self, invoice_payload: str) -> TelegramInvoicePayload:
        if not isinstance(invoice_payload, str):
            raise PaymentEvidenceValidationError("Telegram invoice payload must be a string")
        try:
            payload_bytes = invoice_payload.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise PaymentEvidenceValidationError("Invalid Telegram invoice payload") from exc
        if not 1 <= len(payload_bytes) <= 128 or not invoice_payload.isascii():
            raise PaymentEvidenceValidationError("Invalid Telegram invoice payload")

        parts = invoice_payload.split(".")
        if len(parts) != 5 or parts[0] != _INVOICE_PAYLOAD_VERSION:
            raise PaymentEvidenceValidationError("Invalid Telegram invoice payload")
        raw_order_id = _base64url_decode(parts[1], field_name="Telegram invoice order id")
        if len(raw_order_id) != 16:
            raise PaymentEvidenceValidationError("Invalid Telegram invoice order id")
        try:
            order_id = UUID(bytes=raw_order_id)
        except ValueError as exc:
            raise PaymentEvidenceValidationError("Invalid Telegram invoice order id") from exc
        telegram_user_id = _base36_decode(parts[2], field_name="Telegram invoice user id")
        stars_amount = _base36_decode(parts[3], field_name="Telegram invoice Stars amount")
        presented_signature = _base64url_decode(parts[4], field_name="Telegram invoice signature")
        if len(presented_signature) != sha256().digest_size:
            raise PaymentEvidenceValidationError("Invalid Telegram invoice signature")
        unsigned_payload = ".".join(parts[:4]).encode("ascii")
        expected_signature = hmac.new(self._payload_secret(), unsigned_payload, sha256).digest()
        if not hmac.compare_digest(presented_signature, expected_signature):
            raise PaymentEvidenceValidationError("Invalid Telegram invoice signature")
        return TelegramInvoicePayload(
            order_id=order_id,
            telegram_user_id=telegram_user_id,
            stars_amount=stars_amount,
            raw_payload=invoice_payload,
        )

    def validate_frozen_invoice_payload(
        self,
        *,
        invoice_payload: str,
        expected_order_id: UUID,
        expected_telegram_user_id: int | str,
        expected_stars_amount: int,
    ) -> TelegramInvoicePayload:
        """Require a byte-for-byte canonical payload for the frozen payment facts."""

        parsed = self._parse_invoice_payload(invoice_payload)
        expected_payload = self.build_invoice_payload(
            order_id=expected_order_id,
            telegram_user_id=expected_telegram_user_id,
            stars_amount=expected_stars_amount,
        )
        if not hmac.compare_digest(invoice_payload.encode("ascii"), expected_payload.encode("ascii")):
            raise PaymentEvidenceValidationError("Telegram invoice payload does not match frozen payment facts")
        return parsed

    def create_payment(self, request: PaymentCreationRequest) -> PaymentCreation:
        """Create local send-invoice instructions only when all security config exists."""

        if request.resolved_provider_currency != TELEGRAM_STARS_CURRENCY:
            raise ValueError("Telegram Stars requires provider_currency='XTR'")
        if request.provider_amount is None:
            raise ValueError("Telegram Stars requires an explicit frozen provider_amount in Stars")
        stars_amount = request.resolved_provider_amount
        try:
            telegram_user_id = _telegram_user_id(request.customer_reference)
        except ValueError as exc:
            raise ValueError("customer_reference must be the frozen Telegram user id") from exc

        if not self.is_configured:
            return PaymentCreation(
                provider_namespace=self.provider_namespace,
                merchant_reference=request.merchant_reference,
                provider_amount=stars_amount,
                provider_currency=TELEGRAM_STARS_CURRENCY,
                instructions=instruction_snapshot(
                    {
                        "action": "telegram_stars_configuration_required",
                        "merchant_reference": request.merchant_reference,
                        "payment_confirmation_source": "authenticated_successful_payment_only",
                    }
                ),
                availability=PaymentInitiationAvailability.DISABLED,
                unavailable_reason=(
                    "Telegram Stars requires bot token, webhook secret, and invoice payload secret."
                ),
            )

        invoice_payload = self.build_invoice_payload(
            order_id=request.order_id,
            telegram_user_id=telegram_user_id,
            stars_amount=stars_amount,
        )
        return PaymentCreation(
            provider_namespace=self.provider_namespace,
            merchant_reference=request.merchant_reference,
            provider_amount=stars_amount,
            provider_currency=TELEGRAM_STARS_CURRENCY,
            instructions=instruction_snapshot(
                {
                    "action": "send_telegram_stars_invoice",
                    "invoice_payload": invoice_payload,
                    "telegram_user_id": telegram_user_id,
                    "provider_amount": stars_amount,
                    "provider_currency": TELEGRAM_STARS_CURRENCY,
                    "merchant_reference": request.merchant_reference,
                    "payment_confirmation_source": "authenticated_successful_payment_only",
                }
            ),
        )

    def verify_webhook_secret(self, presented_secret: str | None) -> None:
        """Constant-time validation of Telegram's configured webhook-secret header."""

        expected_secret = self.configuration.webhook_secret
        if not expected_secret:
            raise PaymentAdapterConfigurationError(
                "Telegram webhook authentication is disabled because no webhook secret is configured."
            )
        if not isinstance(presented_secret, str) or not presented_secret:
            raise PaymentEvidenceValidationError("Missing Telegram webhook secret")
        if not hmac.compare_digest(presented_secret, expected_secret):
            raise PaymentEvidenceValidationError("Invalid Telegram webhook secret")

    def validate_pre_checkout(
        self,
        *,
        invoice_payload: str,
        total_amount: int,
        currency: str,
        telegram_user_id: int | str,
        expected_order_id: UUID,
        expected_telegram_user_id: int | str,
        expected_stars_amount: int,
    ) -> TelegramInvoicePayload:
        """Validate pre-checkout facts; callers decide whether to acknowledge them."""

        if isinstance(total_amount, bool) or not isinstance(total_amount, int) or total_amount <= 0:
            raise PaymentEvidenceValidationError("Telegram pre-checkout amount must be a positive integer")
        if normalize_currency(currency) != TELEGRAM_STARS_CURRENCY:
            raise PaymentEvidenceValidationError("Telegram pre-checkout currency must be XTR")
        try:
            actual_user_id = _telegram_user_id(telegram_user_id)
            frozen_user_id = _telegram_user_id(expected_telegram_user_id)
        except ValueError as exc:
            raise PaymentEvidenceValidationError(str(exc)) from exc
        if actual_user_id != frozen_user_id:
            raise PaymentEvidenceValidationError("Telegram pre-checkout user does not match the frozen user")
        if total_amount != expected_stars_amount:
            raise PaymentEvidenceValidationError("Telegram pre-checkout amount does not match the frozen price")
        return self.validate_frozen_invoice_payload(
            invoice_payload=invoice_payload,
            expected_order_id=expected_order_id,
            expected_telegram_user_id=frozen_user_id,
            expected_stars_amount=expected_stars_amount,
        )

    def validate_successful_payment(
        self,
        *,
        merchant_reference: str,
        telegram_payment_charge_id: str,
        invoice_payload: str,
        total_amount: int,
        currency: str,
        telegram_user_id: int | str,
        expected_order_id: UUID,
        expected_telegram_user_id: int | str,
        expected_stars_amount: int,
    ) -> TelegramStarsPaymentEvidence:
        """Validate callback facts and return evidence only, never a settlement result."""

        payload = self.validate_pre_checkout(
            invoice_payload=invoice_payload,
            total_amount=total_amount,
            currency=currency,
            telegram_user_id=telegram_user_id,
            expected_order_id=expected_order_id,
            expected_telegram_user_id=expected_telegram_user_id,
            expected_stars_amount=expected_stars_amount,
        )
        return TelegramStarsPaymentEvidence(
            merchant_reference=merchant_reference,
            telegram_payment_charge_id=telegram_payment_charge_id,
            order_id=payload.order_id,
            telegram_user_id=payload.telegram_user_id,
            stars_amount=payload.stars_amount,
            currency=TELEGRAM_STARS_CURRENCY,
            invoice_payload=payload.raw_payload,
        )

    async def answer_pre_checkout_query(
        self,
        *,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
        timeout_seconds: float = 8.0,
    ) -> TelegramBotApiResult:
        """Explicitly call ``answerPreCheckoutQuery`` using only the standard library.

        Network access is refused unless a bot token and
        ``allow_bot_api_calls=True`` are explicitly configured. Call this only
        after the route/service has persisted enough idempotent state to safely
        handle retries; an acknowledgement is not proof of payment.
        """

        if not self.configuration.can_call_bot_api:
            raise PaymentAdapterConfigurationError(
                "Telegram Bot API calls require a bot token and allow_bot_api_calls=True."
            )
        query_id = normalize_reference(
            pre_checkout_query_id, field_name="pre_checkout_query_id", max_length=256
        )
        if not isinstance(ok, bool):
            raise ValueError("ok must be a boolean")
        if not ok:
            error_message = normalize_reference(
                error_message or "Payment cannot be accepted.",
                field_name="error_message",
                max_length=256,
            )
        elif error_message is not None:
            raise ValueError("error_message is only valid when ok is False")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
            raise ValueError("timeout_seconds must be a positive number")
        if not 0 < float(timeout_seconds) <= 10:
            raise ValueError("timeout_seconds must be greater than 0 and at most 10 seconds")

        payload: dict[str, object] = {"pre_checkout_query_id": query_id, "ok": ok}
        if error_message is not None:
            payload["error_message"] = error_message
        response = await asyncio.to_thread(
            self._post_bot_api_json,
            "answerPreCheckoutQuery",
            payload,
            float(timeout_seconds),
        )
        return TelegramBotApiResult(method="answerPreCheckoutQuery", response=instruction_snapshot(response))

    async def validate_and_answer_pre_checkout_query(
        self,
        *,
        pre_checkout_query_id: str,
        invoice_payload: str,
        total_amount: int,
        currency: str,
        telegram_user_id: int | str,
        expected_order_id: UUID,
        expected_telegram_user_id: int | str,
        expected_stars_amount: int,
        timeout_seconds: float = 8.0,
    ) -> TelegramBotApiResult:
        """Acknowledge only after all frozen facts pass local validation."""

        self.validate_pre_checkout(
            invoice_payload=invoice_payload,
            total_amount=total_amount,
            currency=currency,
            telegram_user_id=telegram_user_id,
            expected_order_id=expected_order_id,
            expected_telegram_user_id=expected_telegram_user_id,
            expected_stars_amount=expected_stars_amount,
        )
        return await self.answer_pre_checkout_query(
            pre_checkout_query_id=pre_checkout_query_id,
            ok=True,
            timeout_seconds=timeout_seconds,
        )

    def _post_bot_api_json(
        self,
        method: str,
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Blocking standard-library HTTP helper, invoked through ``asyncio.to_thread``."""

        bot_token = self.configuration.bot_token
        if not bot_token:
            raise PaymentAdapterConfigurationError("Telegram bot token is not configured.")
        endpoint = f"{self.configuration.bot_api_base_url}/bot{bot_token}/{method}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(dict(payload), separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw_response = response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PaymentAdapterError("Telegram Bot API pre-checkout acknowledgement failed") from exc
        try:
            decoded = json.loads(raw_response)
        except (TypeError, ValueError) as exc:
            raise PaymentAdapterError("Telegram Bot API returned invalid JSON") from exc
        if not isinstance(decoded, dict) or decoded.get("ok") is not True:
            raise PaymentAdapterError("Telegram Bot API rejected the pre-checkout acknowledgement")
        return decoded
