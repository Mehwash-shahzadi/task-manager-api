"""The numbers behind /health and the alert jobs: how many events wait, are dead, or pile up."""

import logging

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.config import settings
from app.database import get_engine
from app.models.outbox import OutboxEvent, OutboxStatus
from app.redis_client import get_redis_client

logger = logging.getLogger(__name__)

STREAM_WARN_FRACTION = (
    0.8  # Log an error when the backlog reaches this part of the limit.
)


# Number of events waiting to be sent. A growing number means the relay is stuck.
def pending_count(engine: Engine | None = None) -> int:
    engine = engine if engine is not None else get_engine()
    with Session(engine) as session:
        return session.exec(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.PENDING)
        ).one()


def stream_backlog(redis: Redis | None = None) -> int | None:
    """Events the worker has not finished: not yet read, plus read but not acknowledged.

    Redis trims the oldest entries when the stream passes EVENT_STREAM_MAXLEN, so a
    backlog close to that limit means unhandled events are about to be lost.
    Returns None if Redis can't tell (it is down, or the stream or group is missing).
    """
    redis = redis if redis is not None else get_redis_client()
    try:
        for group in redis.xinfo_groups(settings.EVENT_STREAM):  # type: ignore[union-attr]
            if group["name"] == settings.CONSUMER_GROUP:
                lag = group.get("lag")
                if lag is None:  # Redis could not work it out.
                    return None
                return int(lag) + int(group["pending"])
    except RedisError:
        return None
    return None


def check_stream_backlog(redis: Redis | None = None) -> int | None:
    """Log an error if the stream is nearly full of unhandled events. Returns the backlog."""
    backlog = stream_backlog(redis)
    limit = settings.EVENT_STREAM_MAXLEN
    if backlog is not None and backlog >= limit * STREAM_WARN_FRACTION:
        logger.error(
            "Event stream is nearly full: %s unhandled events, limit %s. "
            "Redis will drop the oldest unread events. Is the stream worker running?",
            backlog,
            limit,
        )
    return backlog


def dead_count(engine: Engine | None = None) -> int:
    """Events that gave up after too many bad tries. They need a person to look at them."""
    engine = engine if engine is not None else get_engine()
    with Session(engine) as session:
        return session.exec(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.DEAD)
        ).one()


def check_dead(engine: Engine | None = None) -> int:
    """Log an error if there are dead events. Returns how many there are."""
    dead = dead_count(engine)
    if dead:
        logger.error(
            "%s outbox events are dead (they could not be sent). Look at last_error in the "
            "outbox_event table, then run: python -m app.messaging.replay outbox",
            dead,
        )
    return dead
