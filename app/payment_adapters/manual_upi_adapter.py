"""Manual QR/UPI instruction and proof-evidence boundary.

Submitting a UTR or screenshot is deliberately only evidence for an admin
review queue. This module has no bank-status check and cannot settle a
payment, order, ticket, or ledger entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from app.payment_adapters.base import (
    PaymentCreation,
    PaymentCreationRequest,
    PaymentEvidenceValidationError,
    PaymentInitiationAvailability,
    instruction_snapshot,
    normalize_reference,
)


def _paise_as_inr_text(amount_paise: int) -> str:
    """Format integer paise as a display-only INR string without float math."""

    return f"{amount_paise // 100}.{amount_paise % 100:02d}"


@dataclass(frozen=True, slots=True)
class ManualUPIConfiguration:
    """Destination values that are snapshotted into every manual instruction."""

    upi_id: str | None = None
    payee_name: str | None = None
    qr_image_reference: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("upi_id", "payee_name", "qr_image_reference"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    normalize_reference(value, field_name=field_name, max_length=512),
                )

    @property
    def is_configured(self) -> bool:
        return self.upi_id is not None and self.payee_name is not None


@dataclass(frozen=True, slots=True)
class ManualUPIProofEvidence:
    """Unapproved user-submitted evidence for a later maker/checker decision."""

    merchant_reference: str
    utr: str
    proof_reference: str
    submitted_by: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "merchant_reference",
            normalize_reference(self.merchant_reference, field_name="merchant_reference"),
        )
        object.__setattr__(self, "utr", normalize_reference(self.utr, field_name="utr"))
        object.__setattr__(
            self,
            "proof_reference",
            normalize_reference(self.proof_reference, field_name="proof_reference", max_length=512),
        )
        object.__setattr__(
            self,
            "submitted_by",
            normalize_reference(self.submitted_by, field_name="submitted_by"),
        )


@dataclass(frozen=True, slots=True)
class ManualUPIAdapter:
    """Create frozen UPI instructions and normalise proof without approving it."""

    configuration: ManualUPIConfiguration = ManualUPIConfiguration()
    provider_namespace: str = "manual_upi"

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

    def create_payment(self, request: PaymentCreationRequest) -> PaymentCreation:
        """Build a destination snapshot; it is never evidence of payment receipt."""

        if request.resolved_provider_currency != "INR":
            raise ValueError("Manual UPI supports INR only")
        if request.resolved_provider_amount != request.amount_paise:
            raise ValueError("Manual UPI provider_amount must equal the frozen amount_paise")

        if not self.is_configured:
            return PaymentCreation(
                provider_namespace=self.provider_namespace,
                merchant_reference=request.merchant_reference,
                provider_amount=request.resolved_provider_amount,
                provider_currency="INR",
                instructions=instruction_snapshot(
                    {
                        "action": "manual_upi_configuration_required",
                        "merchant_reference": request.merchant_reference,
                        "requires_admin_review": True,
                        "proof_is_not_settlement": True,
                    }
                ),
                availability=PaymentInitiationAvailability.DISABLED,
                unavailable_reason="Manual UPI destination is not configured.",
            )

        assert self.configuration.upi_id is not None
        assert self.configuration.payee_name is not None
        upi_uri = "upi://pay?" + urlencode(
            {
                "pa": self.configuration.upi_id,
                "pn": self.configuration.payee_name,
                "am": _paise_as_inr_text(request.amount_paise),
                "cu": "INR",
                "tn": request.merchant_reference,
            }
        )
        return PaymentCreation(
            provider_namespace=self.provider_namespace,
            merchant_reference=request.merchant_reference,
            provider_amount=request.amount_paise,
            provider_currency="INR",
            instructions=instruction_snapshot(
                {
                    "action": "pay_then_submit_proof_for_review",
                    "upi_id": self.configuration.upi_id,
                    "payee_name": self.configuration.payee_name,
                    "qr_image_reference": self.configuration.qr_image_reference,
                    "upi_uri": upi_uri,
                    "amount_paise": request.amount_paise,
                    "display_amount": _paise_as_inr_text(request.amount_paise),
                    "currency": "INR",
                    "merchant_reference": request.merchant_reference,
                    "requires_admin_review": True,
                    "proof_is_not_settlement": True,
                }
            ),
        )

    def build_proof_evidence(
        self,
        *,
        merchant_reference: str,
        utr: str,
        proof_reference: str,
        submitted_by: str,
    ) -> ManualUPIProofEvidence:
        """Return unapproved evidence for persistence and later admin review.

        Duplicate UTR detection, order locking, audit logging, and every
        approval/rejection transition belong to the database-backed service.
        """

        try:
            return ManualUPIProofEvidence(
                merchant_reference=merchant_reference,
                utr=utr,
                proof_reference=proof_reference,
                submitted_by=submitted_by,
            )
        except ValueError as exc:
            raise PaymentEvidenceValidationError(str(exc)) from exc
