"""Send dead events back to be tried again.

Run: python -m app.messaging.replay [stream|outbox|all]   (default: all)

  stream  the dead-letter stream (events the worker could not handle) goes back to the stream
  outbox  dead outbox rows (events the relay could not send) go back to "pending"
"""

import logging
import sys
from typing import Any, cast

from redis import Redis
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.config import settings
from app.database import get_engine
from app.messaging.consumer import DEAD_STREAM, get_consumer_redis
from app.models.outbox import OutboxEvent, OutboxStatus

logger = logging.getLogger(__name__)

# These two fields are the original event. "error" and "original_id" were added when it died.
ORIGINAL_FIELDS = ("event_type", "payload")


def replay_dead(limit: int = 100, redis: Redis | None = None) -> int:
    """Move up to `limit` dead events back to the main stream. Returns how many moved."""
    redis = redis if redis is not None else get_consumer_redis()
    moved = 0
    # redis-py's hints allow an Awaitable here, but this is the sync client.
    entries = cast(
        list[tuple[str, dict[str, str]]],
        redis.xrange(DEAD_STREAM, count=limit),
    )
    for entry_id, fields in entries:
        original = {k: v for k, v in fields.items() if k in ORIGINAL_FIELDS}
        # One transaction: the event is never in both streams, or in neither.
        pipe = redis.pipeline(transaction=True)
        pipe.xadd(
            settings.EVENT_STREAM,
            cast(dict[Any, Any], original),
            maxlen=settings.EVENT_STREAM_MAXLEN,
            approximate=True,
        )
        pipe.xdel(DEAD_STREAM, entry_id)
        pipe.execute()
        moved += 1
    return moved


def replay_dead_outbox(limit: int = 100, engine: Engine | None = None) -> int:
    """Set up to `limit` dead outbox rows back to pending. Returns how many were reset."""
    engine = engine if engine is not None else get_engine()
    with Session(engine) as session:
        now = session.exec(select(func.now())).one()
        rows = session.exec(
            select(OutboxEvent)
            .where(OutboxEvent.status == OutboxStatus.DEAD)
            .order_by(col(OutboxEvent.created_at))
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.status = OutboxStatus.PENDING
            row.bad_attempts = (
                0  # A fresh set of tries. last_error stays for the record.
            )
            row.next_attempt_at = now
            session.add(row)
        session.commit()
        return len(rows)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what not in ("stream", "outbox", "all"):
        sys.exit("Usage: python -m app.messaging.replay [stream|outbox|all]")
    if what in ("stream", "all"):
        print(f"Replayed {replay_dead()} dead stream events")
    if what in ("outbox", "all"):
        print(f"Reset {replay_dead_outbox()} dead outbox rows to pending")
