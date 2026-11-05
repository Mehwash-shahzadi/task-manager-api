from functools import lru_cache

from redis import Redis

from app.config import settings


def make_redis(socket_timeout: float, url: str | None = None) -> Redis:
    """One place that builds every Redis client, so they all get the same safety settings."""
    return Redis.from_url(
        url or settings.REDIS_URL,
        decode_responses=True,
        socket_timeout=socket_timeout,  # Wait for Redis to reply.
        socket_connect_timeout=settings.REDIS_TIMEOUT,  # Wait for the connection to open.
        health_check_interval=30,  # Test a connection that was idle for 30 s before using it.
        max_connections=settings.REDIS_MAX_CONNECTIONS,  # Never open more than this.
    )


# Each client is built on its first use, not when this module is imported.
@lru_cache
def get_redis_client() -> Redis:
    """Streams, job ids and health. This server never evicts data."""
    return make_redis(settings.REDIS_TIMEOUT)


@lru_cache
def get_cache_client() -> Redis:
    """Only cached tasks. This server may drop old keys when it is full."""
    return make_redis(settings.REDIS_TIMEOUT, settings.CACHE_REDIS_URL)
