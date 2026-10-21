import json
from typing import Any
from uuid import UUID

from redis import Redis, RedisError

from app.config import setting

redis_client = Redis.from_url(setting.REDIS_URL, decode_responses=True)

PREFIX = "tasks:"
LIST_KEY = f"{PREFIX}list"


def task_key(id: UUID) -> str:
    return f"{PREFIX}{id}"


# If Redis is down, the API falls back to the database.
def cache_get(key: str) -> Any | None:
    try:
        value = redis_client.get(key)
    except RedisError:
        return None
    return None if value is None else json.loads(value)#JSON string ko Python data mein convert karta hai.


def cache_set(key: str, value: Any) -> None:
    try:
        redis_client.set(key, json.dumps(value), ex=setting.CACHE_TTL)
    except RedisError:
        pass


def cache_invalidate(*keys: str) -> None:
    try:
        redis_client.delete(*keys)
    except RedisError:
        pass
