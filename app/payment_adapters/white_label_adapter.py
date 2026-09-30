"""Safe boundary for a future white-label payment provider.

This generic adapter deliberately has no provider-specific API call. It can
authenticate a signed raw webhook when a deployment supplies a secret, but a
concrete provider integration must own checkout creation and provider-specific
response parsing. That prevents configuration from being mistaken for payment
confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
from hashlib import sha256

from app.payment_adapters.base import (
    AuthenticatedWebhook,
    PaymentAdapterConfigurationError,
    PaymentCreation,
    PaymentCreationRequest,
    PaymentEvidenceValidationError,
    PaymentInitiationAvailability,
    authenticated_webhook,
    instruction_snapshot,
    merchant_reference_for_order,
    normalize_reference,
)


@dataclass(frozen=True, slots=True)
class WhiteLabelAdapter:
    """Provider-neutral webhook authenticator and safe initiation placeholder."""

    signing_secret: str | None = None
    provider_namespace: str = "white_label"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_namespace",
            normalize_reference(self.provider_namespace, field_name="provider_namespace", max_length=64),
        )
        if self.signing_secret is not None:
            if not isinstance(self.signing_secret, str) or not self.signing_secret.strip():
                raise ValueError("signing_secret must be a non-blank string when configured")
            object.__setattr__(self, "signing_secret", self.signing_secret.strip())

    @property
    def webhook_verification_enabled(self) -> bool:
        return self.signing_secret is not None

    @property
    def supports_checkout_creation(self) -> bool:
        """A generic boundary cannot safely manufacture a provider checkout."""

        return False

    @property
    def supports_refunds(self) -> bool:
        return False

    def merchant_reference_for_order(self, order_id: object) -> str:
        """Return a provider-neutral reference with no provider-specific prefix."""

        return merchant_reference_for_order(order_id)  # type: ignore[arg-type]

    def create_payment(self, request: PaymentCreationRequest) -> PaymentCreation:
        """Return explicit disabled/unsupported instructions without a fake checkout."""

        # The orchestrator has already minted a unique immutable reference for
        # this exact attempt. Preserve it even while checkout creation remains
        # unsupported, so a later concrete adapter cannot silently switch the
        # provider identity to an order-level reference.
        merchant_reference = normalize_reference(
            request.merchant_reference, field_name="merchant_reference", max_length=160
        )
        common_instructions = {
            "action": "configure_concrete_white_label_provider",
            "order_id": str(request.order_id),
            "merchant_reference": merchant_reference,
            "requires_signed_webhook": True,
            "payment_confirmation_source": "signed_provider_event_only",
        }
        if not self.webhook_verification_enabled:
            return PaymentCreation(
                provider_namespace=self.provider_namespace,
                merchant_reference=merchant_reference,
                provider_amount=request.resolved_provider_amount,
                provider_currency=request.resolved_provider_currency,
                instructions=instruction_snapshot(common_instructions),
                availability=PaymentInitiationAvailability.DISABLED,
                unavailable_reason="White-label webhook signing secret is not configured.",
            )
        return PaymentCreation(
            provider_namespace=self.provider_namespace,
            merchant_reference=merchant_reference,
            provider_amount=request.resolved_provider_amount,
            provider_currency=request.resolved_provider_currency,
            instructions=instruction_snapshot(common_instructions),
            availability=PaymentInitiationAvailability.UNSUPPORTED,
            unavailable_reason=(
                "A concrete white-label provider adapter must implement checkout creation; "
                "this generic boundary only authenticates callbacks."
            ),
        )

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str | None) -> None:
        """Authenticate the exact raw body with HMAC-SHA256.

        Authentication does not imply that callback amount, order mapping, or
        provider event id is valid. The orchestration service must persist and
        validate those facts before considering any state transition.
        """

        if not self.signing_secret:
            raise PaymentAdapterConfigurationError(
                "White-label webhook verification is disabled because no signing secret is configured."
            )
        if not isinstance(raw_body, bytes):
            raise TypeError("raw_body must be bytes")
        if not isinstance(signature, str) or not signature.strip():
            raise PaymentEvidenceValidationError("Missing white-label webhook signature")

        presented = signature.strip()
        if presented.lower().startswith("sha256="):
            presented = presented.split("=", 1)[1]
        expected = hmac.new(
            self.signing_secret.encode("utf-8"), raw_body, sha256
        ).hexdigest()
        if not hmac.compare_digest(presented.lower(), expected):
            raise PaymentEvidenceValidationError("Invalid white-label webhook signature")

    def authenticate_webhook(self, *, raw_body: bytes, signature: str | None) -> AuthenticatedWebhook:
        """Return only a raw-body digest after signature authentication succeeds."""

        self.verify_webhook_signature(raw_body=raw_body, signature=signature)
        return authenticated_webhook(provider_namespace=self.provider_namespace, raw_body=raw_body)
