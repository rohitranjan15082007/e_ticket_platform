"""Tests for password hashing and token integrity."""

import pytest

from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.exceptions import AuthenticationError


def test_password_hash_verifies_only_the_original_password() -> None:
    encoded = hash_password("correct-horse-battery-staple")

    assert verify_password("correct-horse-battery-staple", encoded)
    assert not verify_password("wrong-horse-battery-staple", encoded)


def test_access_token_round_trip_preserves_subject_and_roles() -> None:
    token = create_access_token("e342b644-0f9e-47f6-a9db-59908380e01e", ["user"])

    assert decode_access_token(token)["roles"] == ["user"]


def test_malformed_token_is_rejected() -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token("not-a-token")
