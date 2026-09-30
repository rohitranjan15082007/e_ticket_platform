"""Bounded dependency checks for orchestration readiness, not payment verification."""

from redis.asyncio import Redis

from app.config import get_settings


async def verify_redis_connection() -> None:
    client = Redis.from_url(get_settings().redis_url)
    try:
        if not await client.ping():
            raise ConnectionError("Redis ping failed")
    finally:
        await client.aclose()
