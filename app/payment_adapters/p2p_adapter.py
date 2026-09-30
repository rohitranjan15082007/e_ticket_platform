"""Concrete verification boundary for signed P2P-provider callbacks.

P2P transfers are made directly to the receiver's frozen destination, so the
platform does not manufacture a payment-initiation success. This adapter
instead owns the provider-facing trust boundary: it authenticates the exact
raw callback body before application services persist or use its evidence.
"""

from dataclasses import dataclass
import hmac
from hashlib import sha256

from app.exceptions import AuthenticationError


@dataclass(frozen=True, slots=True)
class HmacP2PWebhookAdapter:
    """Authenticate HMAC-SHA256 callbacks from a configured P2P provider.

    A deployment must supply the secret out-of-band. This intentionally does
    not contain a fallback or test-only approval path: an absent or invalid
    signature is never payment evidence.
    """

    signing_secret: str

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str | None) -> None:
        if not isinstance(signature, str) or not signature.strip():
            raise AuthenticationError("Missing P2P provider webhook signature")
        presented = signature.strip()
        if presented.lower().startswith("sha256="):
            presented = presented.split("=", 1)[1]
        expected = hmac.new(self.signing_secret.encode("utf-8"), raw_body, sha256).hexdigest()
        if not hmac.compare_digest(presented.lower(), expected):
            raise AuthenticationError("Invalid P2P provider webhook signature")
