import json
import logging
from typing import Any
from uuid import UUID

from redis import Redis, RedisError

from app.config import settings

logger = logging.getLogger(__name__)

# timeout is liye h k Redis atak jaye to requests na latken; connection refuse ho to wo pehle hi jaldi fail ho jata h
redis_client = Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_timeout=settings.REDIS_TIMEOUT,
    socket_connect_timeout=settings.REDIS_TIMEOUT,
)

PREFIX = "tasks:"
LIST_KEY = f"{PREFIX}list"


def task_key(id: UUID) -> str:
    return f"{PREFIX}{id}"


# Redis band ho to API seedha database se data le leti h
def cache_get(key: str) -> Any | None:
    try:
        value = redis_client.get(key)
    except RedisError:
        logger.warning("Redis unavailable, cache_get skipped for %s", key)
        return None
    return None if value is None else json.loads(value)#JSON string ko Python data mein convert karta hai.


def cache_set(key: str, value: Any) -> None:
    try:
        redis_client.set(key, json.dumps(value), ex=settings.CACHE_TTL)
    except RedisError:
        logger.warning("Redis unavailable, cache_set skipped for %s", key)


def cache_invalidate(*keys: str) -> None:
    try:
        redis_client.delete(*keys)
    except RedisError:
        logger.warning("Redis unavailable, cache_invalidate skipped for %s", keys)


def cache_list() -> list[dict[str, Any]]:
    entries = []

    try:
        # scan_iter SCAN use karta h KEYS nahi, aur prefix ki wajah se sirf is app ki keys aati hain
        for key in redis_client.scan_iter(match=f"{PREFIX}*"):
            value = redis_client.get(key)
            if value is None:  # SCAN aur GET ke beech mein expire ho gayi
                continue
            entries.append(
                {"key": key, "ttl": redis_client.ttl(key), "value": json.loads(value)}
            )
    except RedisError:
        logger.warning("Redis unavailable, cache_list returned partial or empty result")

    return entries
