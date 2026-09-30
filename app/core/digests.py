"""Stable digests for signed/event payload correlation, never for passwords."""

import json
from hashlib import sha256
from collections.abc import Mapping

from app.core.idempotency import canonical_payload


def canonical_payload_digest(payload: Mapping[str, object]) -> str:
    """Hash a JSON-safe payload with a deterministic key order."""

    encoded = json.dumps(canonical_payload(payload), sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
