"""Which export jobs we started, and the "one export at a time" lock.

Both live on the main Redis server (it never evicts data). Celery alone cannot tell a job
that is waiting from an id that never existed, so every job id is written down here.
"""

import logging

from redis.exceptions import RedisError

from app.config import settings
from app.redis_client import get_redis_client

logger = logging.getLogger(__name__)

JOB_PREFIX = f"{settings.KEY_PREFIX}job:"
EXPORT_LOCK_KEY = f"{settings.KEY_PREFIX}export:running"

# Delete the lock only if it still belongs to this job (it may have expired and been taken).
_RELEASE_IF_MINE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def remember_job(job_id: str) -> None:
    """Remember a job id we started. Raises RedisError if Redis is down."""
    get_redis_client().set(JOB_PREFIX + job_id, 1, ex=settings.JOB_RESULT_TTL_SECONDS)


def forget_job(job_id: str) -> None:
    """Undo remember_job (the job could not be queued). Never raises."""
    try:
        get_redis_client().delete(JOB_PREFIX + job_id)
    except RedisError:
        logger.warning("Could not forget job %s; its id will expire by itself", job_id)


def job_exists(job_id: str) -> bool:
    """True if we started this job."""
    return bool(get_redis_client().exists(JOB_PREFIX + job_id))


def claim_export(job_id: str) -> str | None:
    """Try to become the one running export.

    Returns None if this job got the lock, or the id of the export that already holds it.
    Raises RedisError if Redis is down.
    """
    redis = get_redis_client()
    for _ in range(2):  # The holder may expire between our two calls; try once more.
        if redis.set(EXPORT_LOCK_KEY, job_id, nx=True, ex=settings.EXPORT_LOCK_SECONDS):
            return None
        holder = redis.get(EXPORT_LOCK_KEY)
        if holder is not None:
            return str(holder)
    return None


def release_export(job_id: str) -> None:
    """Give the lock back. Never raises: if Redis is down, the lock expires by itself."""
    try:
        get_redis_client().eval(_RELEASE_IF_MINE, 1, EXPORT_LOCK_KEY, job_id)
    except RedisError:
        logger.warning("Could not release the export lock; it expires in time")
