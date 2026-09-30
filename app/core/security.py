"""Password and JWT primitives used by the authentication service."""

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt import InvalidTokenError

from app.config import get_settings
from app.exceptions import AuthenticationError

PASSWORD_ITERATIONS = 600_000


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 and a random salt."""

    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters long")
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${_b64encode(salt)}${_b64encode(derived)}"


def verify_password(password: str, encoded_password: str) -> bool:
    """Compare a candidate password to its stored hash in constant time."""

    try:
        algorithm, iterations, salt_text, expected_text = encoded_password.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _b64decode(salt_text), int(iterations)
        )
        return hmac.compare_digest(actual, _b64decode(expected_text))
    except (TypeError, ValueError):
        return False


def create_access_token(subject: str, roles: list[str]) -> str:
    """Create a short-lived, signed access token for a known user."""

    settings = get_settings()
    issued_at = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "roles": roles,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.access_token_expire_minutes),
        "jti": secrets.token_urlsafe(18),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Validate and decode an access token, without accepting malformed claims."""

    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except InvalidTokenError as error:
        raise AuthenticationError("Invalid or expired access token") from error
    if not isinstance(payload.get("sub"), str) or not isinstance(payload.get("roles"), list):
        raise AuthenticationError("Invalid access token claims")
    return payload
