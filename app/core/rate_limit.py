"""Distributed, fail-closed limits for public authentication mutations."""

import asyncio
import hashlib
import hmac
import os
from dataclasses import dataclass
from ipaddress import ip_address

from fastapi import Request
from redis.asyncio import Redis

from app.config import get_settings
from app.exceptions import AppError

# Fixed-window counters are incremented together in one Redis operation. Every
# key uses the same hash tag so the script also works on a Redis Cluster slot.
_LIMIT_SCRIPT = """
local blocked_ms = 0
for index = 1, #KEYS do
    local count = redis.call('INCR', KEYS[index])
    local ttl = redis.call('PTTL', KEYS[index])
    if count == 1 or ttl < 0 then
        ttl = tonumber(ARGV[index * 2])
        redis.call('PEXPIRE', KEYS[index], ttl)
    end
    if count > tonumber(ARGV[index * 2 - 1]) and ttl > blocked_ms then
        blocked_ms = ttl
    end
end
return blocked_ms
"""

LOGIN_ACCOUNT_LIMIT = 5
LOGIN_CLIENT_LIMIT = 30
FALLBACK_LOGIN_CLIENT_LIMIT = 300
LOGIN_WINDOW_SECONDS = 15 * 60
REGISTER_ACCOUNT_LIMIT = 3
REGISTER_CLIENT_LIMIT = 10
FALLBACK_REGISTER_CLIENT_LIMIT = 100
REGISTER_WINDOW_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class _Limit:
    key: str
    maximum: int
    window_seconds: int


def _private_key(action: str, dimension: str, value: str) -> str:
    """Keep account and network identifiers out of Redis key names."""

    secret = get_settings().jwt_secret.encode("utf-8")
    message = f"auth-limit-v1\x1f{action}\x1f{dimension}\x1f{value}".encode("utf-8")
    digest = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return f"ticket:{{auth-limits}}:{action}:{dimension}:{digest}"


def _client_identity(request: Request) -> tuple[str, bool]:
    """Use Vercel's overwritten client IP only in an identified Vercel runtime.

    On other hosts, forwarding headers are untrusted and the ASGI peer is used.
    A reverse proxy on a non-Vercel host can aggregate clients under one peer;
    that path uses a higher client cap while the account cap remains strict.
    """

    if os.environ.get("VERCEL") == "1":
        forwarded = request.headers.get("x-forwarded-for")
        try:
            if not forwarded:
                raise ValueError("Missing Vercel client address")
            return str(ip_address(forwarded.strip())), True
        except ValueError as error:
            raise AppError(
                "AUTH_RATE_LIMIT_UNAVAILABLE", "Authentication is temporarily unavailable", 503
            ) from error
    return request.client.host if request.client is not None else "unknown", False


async def check_auth_rate_limit(*, action: str, email: str, request: Request) -> None:
    """Count a validated auth attempt before executing any account mutation.

    Untrusted forwarding headers must not let a caller change its own
    rate-limit identity outside the Vercel deployment boundary.
    """

    client_address, trusted_client_address = _client_identity(request)
    if action == "login":
        account_maximum, client_maximum, window_seconds = (
            LOGIN_ACCOUNT_LIMIT,
            LOGIN_CLIENT_LIMIT if trusted_client_address else FALLBACK_LOGIN_CLIENT_LIMIT,
            LOGIN_WINDOW_SECONDS,
        )
    elif action == "register":
        account_maximum, client_maximum, window_seconds = (
            REGISTER_ACCOUNT_LIMIT,
            REGISTER_CLIENT_LIMIT if trusted_client_address else FALLBACK_REGISTER_CLIENT_LIMIT,
            REGISTER_WINDOW_SECONDS,
        )
    else:
        raise ValueError("Unsupported authentication rate-limit action")

    limits = (
        _Limit(_private_key(action, "account", email.casefold()), account_maximum, window_seconds),
        _Limit(_private_key(action, "client", client_address), client_maximum, window_seconds),
    )
    arguments = [value for limit in limits for value in (limit.maximum, limit.window_seconds * 1000)]
    try:
        async with Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=1.0, socket_timeout=1.0
        ) as client:
            result = await asyncio.wait_for(
                client.eval(_LIMIT_SCRIPT, len(limits), *(limit.key for limit in limits), *arguments),
                timeout=2.0,
            )
        blocked_ms = int(result)
        if blocked_ms < 0:
            raise ValueError("Invalid rate-limit response")
    except Exception as error:
        # No Redis counter means no safe authority to allow this auth mutation.
        raise AppError(
            "AUTH_RATE_LIMIT_UNAVAILABLE", "Authentication is temporarily unavailable", 503
        ) from error

    if blocked_ms:
        raise AppError("AUTH_RATE_LIMITED", "Too many authentication attempts; try later", 429)
