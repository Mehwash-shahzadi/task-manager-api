import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from redis import Redis
from redis.exceptions import DataError, RedisError
from sqlalchemy import delete, func
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.config import settings
from app.database import get_engine
from app.models.outbox import OutboxEvent, OutboxStatus
from app.redis_client import get_redis_client

logger = logging.getLogger(__name__)


MAX_BACKOFF_SECONDS = 60
MAX_ERROR_LENGTH = 500  # Longest error text saved in last_error.


# Wait longer after each failed attempt: 2, 4, 8, 16, 32, up to 60 seconds.
def backoff_seconds(attempts: int) -> int:
    return min(2 ** min(attempts, 6), MAX_BACKOFF_SECONDS)


CLAIM_LEASE_SECONDS = 60  # How long a claimed row is hidden from other relay runs.


@dataclass(frozen=True)
class ClaimedEvent:
    """A copy of an outbox row, so the relay can work after the database lock is gone."""

    id: UUID
    event_type: str
    payload: dict[str, Any]


def database_now(session: Session) -> datetime:
    """The time from Postgres, so every server uses the same clock."""
    return session.exec(select(func.now())).one()


def is_bad_event(exc: Exception) -> bool:
    """True if the event itself is broken, False if Redis is the problem."""
    return isinstance(exc, (DataError, TypeError, ValueError))


# Step 1: a short transaction. Lock the due rows, hide them for a while, and let go.
def claim_events(engine: Engine | None = None) -> list[ClaimedEvent]:
    engine = engine if engine is not None else get_engine()
    with Session(engine) as session:
        now = database_now(session)
        # Skip rows locked by another relay process.
        rows = session.exec(
            select(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.PENDING)
            .where(col(OutboxEvent.next_attempt_at) <= now)
            .order_by(col(OutboxEvent.created_at))
            .limit(settings.OUTBOX_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        ).all()

        claimed = []
        for row in rows:
            claimed.append(ClaimedEvent(row.id, row.event_type, row.payload))
            # The lease: until it ends, no other relay run picks this row up.
            # If this run crashes, the row comes back by itself when the lease ends.
            row.next_attempt_at = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
            session.add(row)

        session.commit()  # The locks are released here, before any Redis call.
    return claimed


# Step 2: no database transaction is open while we talk to Redis.
def send_events(
    events: list[ClaimedEvent], redis: Redis | None = None
) -> dict[UUID, Exception | None]:
    """Send each event. The result maps an event id to None (sent) or the error."""
    redis = redis if redis is not None else get_redis_client()
    results: dict[UUID, Exception | None] = {}
    for event in events:
        try:
            redis.xadd(
                settings.EVENT_STREAM,
                {
                    "event_type": event.event_type,
                    "payload": json.dumps(event.payload),
                },
                maxlen=settings.EVENT_STREAM_MAXLEN,
                approximate=True,
            )
        except (DataError, TypeError, ValueError) as exc:
            results[event.id] = exc  # The event is broken. Go on with the next one.
        except RedisError as exc:
            results[event.id] = exc
            break  # Redis is in trouble. Do not try the rest of the batch.
        else:
            results[event.id] = None
    return results


def record_failure(row: OutboxEvent, exc: Exception, now: datetime) -> None:
    """What a failed attempt always changes: the count, the error text and the retry time."""
    row.attempts += 1
    row.last_error = str(exc)[:MAX_ERROR_LENGTH]
    row.next_attempt_at = now + timedelta(seconds=backoff_seconds(row.attempts))


# Step 3: a short transaction. Write down what happened to each row.
def record_results(
    events: list[ClaimedEvent],
    results: dict[UUID, Exception | None],
    engine: Engine | None = None,
) -> int:
    engine = engine if engine is not None else get_engine()
    sent = 0
    with Session(engine) as session:
        now = database_now(session)

        for event in events:
            row = session.get(OutboxEvent, event.id)
            if row is None:
                continue  # Cleaned up meanwhile.

            if event.id not in results:
                row.next_attempt_at = now  # Not tried: give the lease back.
            elif (exc := results[event.id]) is None:
                row.status = OutboxStatus.SENT
                row.sent_at = now
                sent += 1
            else:
                record_failure(row, exc, now)
                if is_bad_event(exc):
                    # Invalid event data: retry a few times, then mark it DEAD.
                    row.bad_attempts += 1
                    if row.bad_attempts >= settings.OUTBOX_MAX_BAD_ATTEMPTS:
                        row.status = OutboxStatus.DEAD
                        logger.error("Outbox event %s is dead: %s", row.id, exc)
                # A Redis problem is not counted in bad_attempts, so it never kills the event.

            session.add(row)

        session.commit()
    return sent


# Send due pending events to Redis Stream and return the number sent.
def relay_once(engine: Engine | None = None, redis: Redis | None = None) -> int:
    """Pass an engine and a Redis client to use others than the app's own (tests do)."""
    engine = engine if engine is not None else get_engine()
    redis = redis if redis is not None else get_redis_client()
    events = claim_events(engine)
    if not events:
        return 0
    return record_results(events, send_events(events, redis), engine)


CLEANUP_BATCH_SIZE = 1000


# Delete old SENT events according to the retention period, in small batches.
def cleanup_sent(engine: Engine | None = None) -> int:
    engine = engine if engine is not None else get_engine()
    total = 0

    with Session(engine) as session:
        cutoff = session.exec(select(func.now())).one() - timedelta(
            days=settings.OUTBOX_RETENTION_DAYS
        )
        while True:
            ids = session.exec(
                select(OutboxEvent.id)
                .where(OutboxEvent.status == OutboxStatus.SENT)
                .where(col(OutboxEvent.sent_at) < cutoff)
                .limit(CLEANUP_BATCH_SIZE)
            ).all()
            if not ids:
                break
            session.exec(delete(OutboxEvent).where(col(OutboxEvent.id).in_(ids)))
            session.commit()
            total += len(ids)

    return total


# Delete dead rows that nobody replayed for the retention time (same days as sent rows).
def cleanup_dead(engine: Engine | None = None) -> int:
    engine = engine if engine is not None else get_engine()
    total = 0

    with Session(engine) as session:
        cutoff = session.exec(select(func.now())).one() - timedelta(
            days=settings.OUTBOX_RETENTION_DAYS
        )
        while True:
            ids = session.exec(
                select(OutboxEvent.id)
                .where(OutboxEvent.status == OutboxStatus.DEAD)
                .where(col(OutboxEvent.updated_at) < cutoff)  # When it became dead.
                .limit(CLEANUP_BATCH_SIZE)
            ).all()
            if not ids:
                break
            session.exec(delete(OutboxEvent).where(col(OutboxEvent.id).in_(ids)))
            session.commit()
            total += len(ids)

    return total
