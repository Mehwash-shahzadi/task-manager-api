import json
import logging
from datetime import timedelta

from redis import ConnectionError as RedisConnectionError
from redis import TimeoutError as RedisTimeoutError
from sqlalchemy import delete
from sqlmodel import Session, select

from app.cache import redis_client
from app.config import settings
from app.database import engine
import threading
import time
from app.models.base import current_time
from app.models.outbox import DEAD, PENDING, SENT, OutboxEvent

logger = logging.getLogger(__name__)


# agli try se pehle kitne second ruknna h: 2, 4, 8, 16, 32, phir max 60.
# andar wala min number ko chota rakhta h, lambi outage ke baad bhi.
def backoff_seconds(attempts: int) -> int:
    return min(2 ** min(attempts, 6), 60)


# jin pending events ka time ho gaya h unhe Redis Stream mein bhejta h, aur batata h kitne gaye
def relay_once() -> int:
    sent = 0

    with Session(engine) as session:
        # skip_locked: agar dosra relay saath chal raha ho to wo alag rows uthata h
        rows = session.exec(
            select(OutboxEvent)
            .where(OutboxEvent.status == PENDING)
            .where(OutboxEvent.next_attempt_at <= current_time())
            .order_by(OutboxEvent.created_at)
            .limit(settings.OUTBOX_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        ).all()

        for row in rows:
            try:
                redis_client.xadd(
                    settings.EVENT_STREAM,
                    {"event_type": row.event_type, "payload": json.dumps(row.payload)},
                    maxlen=settings.EVENT_STREAM_MAXLEN,
                    approximate=True,
                )
            except (RedisConnectionError, RedisTimeoutError) as exc:
                # Redis band h: row rakho, har bar zyada intezar karo, isay kabhi dead mark mat karo
                row.attempts += 1
                row.last_error = str(exc)[:500]
                row.next_attempt_at = current_time() + timedelta(
                    seconds=backoff_seconds(row.attempts)
                )
                session.add(row)
                break  # Redis band h to baqi rows try karne ka koi faida nahi
            except Exception as exc:
                # masla event mein hi h: ginti karo aur kuch tries ke baad haar maan lo
                row.attempts += 1
                row.bad_attempts += 1
                row.last_error = str(exc)[:500]
                if row.bad_attempts >= settings.OUTBOX_MAX_BAD_ATTEMPTS:
                    row.status = DEAD
                    logger.error("Outbox event %s is dead: %s", row.id, exc)
                else:
                    row.next_attempt_at = current_time() + timedelta(
                        seconds=backoff_seconds(row.attempts)
                    )
                session.add(row)
            else:
                row.status = SENT
                row.sent_at = current_time()
                session.add(row)
                sent += 1

        # poore batch ka ek hi commit, is se row locks aakhir tak bane rehte hain
        session.commit()

    return sent

# OUTBOX_RETENTION_DAYS se purane sent events delete karta h. Pending aur dead rows rehti hain.
def cleanup_sent() -> int:
    cutoff = current_time() - timedelta(days=settings.OUTBOX_RETENTION_DAYS)

    with Session(engine) as session:
        result = session.exec(
            delete(OutboxEvent)
            .where(OutboxEvent.status == SENT)
            .where(OutboxEvent.sent_at < cutoff)
        )
        session.commit()

    return result.rowcount


CLEANUP_INTERVAL_SECONDS = 3600


# background thread mein chalta h: har OUTBOX_POLL_SECONDS par relay, aur ghante mein ek bar cleanup.
def run_relay(stop: threading.Event) -> None:
    last_cleanup = None

    while not stop.wait(settings.OUTBOX_POLL_SECONDS):
        try:
            relay_once()
        except Exception:
            logger.exception("Outbox relay failed, will try again")

        if last_cleanup is None or time.monotonic() - last_cleanup >= CLEANUP_INTERVAL_SECONDS:
            last_cleanup = time.monotonic()
            try:
                cleanup_sent()
            except Exception:
                logger.exception("Outbox cleanup failed, will try again later")

