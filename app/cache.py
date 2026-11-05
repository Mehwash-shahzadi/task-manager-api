import json
import logging
import threading
from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import Depends
from redis import RedisError

from app.config import settings
from app.redis_client import get_cache_client

logger = logging.getLogger(__name__)


# Timeouts keep requests from hanging if Redis stops answering; a refused connection fails fast.
# Two servers, because they need opposite memory rules:
#   redis_client: streams, job ids, health. Must never lose data (noeviction).
#   cache_client: only cached tasks. When full it drops old keys (allkeys-lru).
CACHE_VERSION = 1  # Raise this number whenever the cached task JSON changes shape.
PREFIX = f"{settings.KEY_PREFIX}tasks:v{CACHE_VERSION}:"

LIST_KEY = f"{PREFIX}list"


def task_key(id: UUID) -> str:
    return f"{PREFIX}{id}"


def _safe(action: str, key: Any, call: Callable[[], Any]) -> Any:
    """Run a Redis call. If Redis is down, log it and return None."""
    try:
        return call()
    except RedisError:
        logger.warning("Redis unavailable, %s skipped for %s", action, key)
        return None


# Hit and miss counts since this process started. Shown by /health.
_stats = {"hits": 0, "misses": 0}
_stats_lock = threading.Lock()


def _count(name: str) -> None:
    with _stats_lock:
        _stats[name] += 1


def cache_stats() -> dict[str, int]:
    with _stats_lock:
        return dict(_stats)


# If Redis is down, the API reads from the database.
def cache_get(key: str) -> Any | None:
    value = _safe("cache_get", key, lambda: get_cache_client().get(key))

    if isinstance(value, (str, bytes, bytearray)):
        try:
            data = json.loads(value)
        except ValueError:
            logger.warning("Bad cached value for %s, treating it as a miss", key)
            cache_invalidate(key)
        else:
            _count("hits")
            return data

    # Not found, Redis down or a bad value: the caller reads the database instead.
    _count("misses")
    logger.debug("cache miss %s", key)
    return None


# A number that goes up every time tasks are cleared from the cache. A reader notes it
# before it reads the database; it may store what it read only if the number is still the
# same. Without this, a slow reader could store an old row after an update cleared it.
GENERATION_KEY = f"{settings.KEY_PREFIX}cache-generation"

# Runs inside Redis in one step, so nothing can change the number between the check and
# the write. KEYS: the key to write, the generation key. ARGV: value, generation, ttl.
_SET_IF_UNCHANGED = """
if (redis.call('GET', KEYS[2]) or '0') == ARGV[2] then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[3]))
    return 1
end
return 0
"""


def cache_token() -> int:
    """The current generation. Read it before you read the database."""
    value = _safe(
        "cache_token", GENERATION_KEY, lambda: get_cache_client().get(GENERATION_KEY)
    )
    return int(value) if isinstance(value, (str, bytes)) else 0


def cache_set(key: str, value: Any, ttl: int, token: int | None = None) -> None:
    """Store a value. With a token, store it only if nothing was cleared since the token."""
    if token is None:
        _safe(
            "cache_set",
            key,
            lambda: get_cache_client().set(key, json.dumps(value), ex=ttl),
        )
        return
    _safe(
        "cache_set",
        key,
        lambda: get_cache_client().eval(
            _SET_IF_UNCHANGED,
            2,
            key,
            GENERATION_KEY,
            json.dumps(value),
            str(token),
            str(ttl),
        ),
    )


def cache_invalidate(*keys: str) -> None:
    _safe("cache_invalidate", keys, lambda: get_cache_client().delete(*keys))


def invalidate_tasks(*task_ids: UUID) -> None:
    """Clear the cached list and the cached copy of each given task."""
    # Raise the generation first: a reader that started before this point can no longer
    # store what it read. Then delete what is already stored.
    _safe(
        "cache_generation",
        GENERATION_KEY,
        lambda: get_cache_client().incr(GENERATION_KEY),
    )
    cache_invalidate(LIST_KEY, *[task_key(i) for i in task_ids])


class TaskCache:
    """The task cache as one object.

    A route asks for it with Depends(get_task_cache) instead of importing the cache
    functions. A test can then hand in a fake with app.dependency_overrides.
    """

    def get(self, key: str) -> Any | None:
        return cache_get(key)

    def token(self) -> int:
        return cache_token()

    def set(self, key: str, value: Any, ttl: int, token: int | None = None) -> None:
        cache_set(key, value, ttl, token)

    def invalidate_tasks(self, *task_ids: UUID) -> None:
        invalidate_tasks(*task_ids)


def get_task_cache() -> TaskCache:
    return TaskCache()


CacheDep = Annotated[TaskCache, Depends(get_task_cache)]


def cache_list() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    try:
        # scan_iter uses SCAN instead of KEYS.
        keys = list(get_cache_client().scan_iter(match=f"{PREFIX}*"))
        if not keys:
            return entries

        # Three calls in total, however many keys there are.
        values = cast(list[Any], get_cache_client().mget(keys))
        pipe = get_cache_client().pipeline()
        for key in keys:
            pipe.ttl(key)
        ttls = pipe.execute()

        for key, value, ttl in zip(keys, values, ttls, strict=True):
            if not isinstance(value, (str, bytes, bytearray)):
                continue  # Expired between SCAN and MGET.
            try:
                entries.append({"key": key, "ttl": ttl, "value": json.loads(value)})
            except ValueError:
                logger.warning("Bad cached value for %s, left out of the list", key)

    except RedisError:
        logger.warning("Redis unavailable, cache_list returned partial or empty result")

    return entries
