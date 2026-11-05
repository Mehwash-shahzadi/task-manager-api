import logging
import os
import socket
from collections.abc import Callable
from functools import lru_cache
from typing import Any, cast

from pydantic import ValidationError
from redis import Redis
from redis.exceptions import ResponseError

from app.config import settings
from app.enums import EventType
from app.redis_client import make_redis
from app.schemas.event import TaskEvent

logger = logging.getLogger(__name__)
DEAD_STREAM = settings.EVENT_STREAM + ":dead"


# What the "seen" key says about an event. Only DONE means the event was really handled.
SEEN_PROCESSING = "processing"
SEEN_DONE = "done"


def seen_key(event_id: str) -> str:
    # The group name is in the key, so two groups never share "seen" markers.
    return f"{settings.KEY_PREFIX}seen:{settings.CONSUMER_GROUP}:{event_id}"


SOCKET_MARGIN_SECONDS = 3  # Extra wait after the block time, so Redis can still reply.
# Socket timeouts are in seconds, but the block time is in ms.
timeout = settings.CONSUMER_BLOCK_MS / 1000 + SOCKET_MARGIN_SECONDS


@lru_cache
def get_consumer_redis() -> Redis:
    """The stream worker's Redis client. Built on first use."""
    return make_redis(timeout)


# Unique name per worker (hostname + process id).
consumer_name = settings.CONSUMER_NAME or f"{socket.gethostname()}-{os.getpid()}"


def ensure_group(redis: Redis | None = None) -> None:
    redis = redis if redis is not None else get_consumer_redis()
    try:
        redis.xgroup_create(
            name=settings.EVENT_STREAM,
            groupname=settings.CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


Handler = Callable[[TaskEvent], None]


def log_event(event: TaskEvent) -> None:
    logger.info(
        "Received event: %s, %s",
        event.event_id,
        event.event_type,
    )


# What to do with each kind of event. A new event type gets its function here.
HANDLERS: dict[EventType, Handler] = {EventType.TASK_CREATED: log_event}


def register_handler(event_type: EventType, handler: Handler) -> None:
    HANDLERS[event_type] = handler


def handle_event(event: TaskEvent) -> None:
    HANDLERS[event.event_type](event)


def send_to_dead_letter(
    stream_name: str,
    message_id: str,
    fields: dict[str, str],
    reason: str,
    redis: Redis | None = None,
) -> None:
    redis = redis if redis is not None else get_consumer_redis()
    dead_fields = fields.copy()
    dead_fields["error"] = reason
    dead_fields["original_id"] = message_id

    # One transaction: the message is never in both streams, or in neither.
    pipe = redis.pipeline(transaction=True)
    pipe.xadd(
        DEAD_STREAM,
        cast(dict[Any, Any], dead_fields),
        maxlen=settings.DEAD_STREAM_MAXLEN,
        approximate=True,
    )
    pipe.xack(stream_name, settings.CONSUMER_GROUP, message_id)
    pipe.execute()


def process_message(
    stream_name: str,
    message_id: str,
    fields: dict[str, str],
    redis: Redis | None = None,
) -> None:
    redis = redis if redis is not None else get_consumer_redis()
    try:
        # The same TaskEvent class the API used to build the event checks it here.
        event = TaskEvent.model_validate_json(fields["payload"])
    except ValidationError as exc:
        reason = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'payload'}: {e['msg']}"
            for e in exc.errors()
        )
        send_to_dead_letter(stream_name, message_id, fields, reason, redis)
        return
    except KeyError:
        send_to_dead_letter(
            stream_name, message_id, fields, "payload field missing", redis
        )
        return

    if event.event_type not in HANDLERS:
        # Retrying cannot fix a missing handler, so do not wait for the retries.
        reason = f"no handler for event type {event.event_type}"
        send_to_dead_letter(stream_name, message_id, fields, reason, redis)
        return

    try:
        key = seen_key(event.event_id)

        # Claim the event with one atomic command (SET NX), before handling it.
        # The claim is short: if this worker crashes, it expires about when the
        # stuck message is taken over, so the retry is not blocked.
        claim_seconds = max(settings.CONSUMER_MIN_IDLE_MS // 1000, 1)
        claimed = redis.set(key, SEEN_PROCESSING, nx=True, ex=claim_seconds)

        if claimed:
            try:
                handle_event(event)
            except Exception:
                redis.delete(key)  # Let the retry run the handler again.
                raise
            # Handled: remember the event for the full "seen" time.
            redis.set(key, SEEN_DONE, ex=settings.CONSUMER_SEEN_TTL_SECONDS)
        elif redis.get(key) != SEEN_DONE:
            # Another worker is handling this event right now (or its claim just ended).
            # Do not acknowledge: if that worker fails, this event must come back.
            return

        redis.xack(stream_name, settings.CONSUMER_GROUP, message_id)
    except Exception as exc:
        logger.exception("Failed to process message %s", message_id)
        if (
            delivery_count(stream_name, message_id, redis)
            >= settings.CONSUMER_MAX_DELIVERIES
        ):
            send_to_dead_letter(stream_name, message_id, fields, str(exc), redis)


def delivery_count(
    stream_name: str, message_id: str, redis: Redis | None = None
) -> int:
    redis = redis if redis is not None else get_consumer_redis()
    """How many times Redis has handed this message to a worker (0 if it is not pending)."""
    pending = cast(
        list[dict[str, Any]],
        redis.xpending_range(
            stream_name, settings.CONSUMER_GROUP, message_id, message_id, 1
        ),
    )
    return pending[0]["times_delivered"] if pending else 0


# Where the next idle-message scan starts. Redis returns "0-0" when the scan is finished.
claim_cursor = "0-0"


def claim_pending_messages(redis: Redis | None = None) -> None:
    global claim_cursor
    redis = redis if redis is not None else get_consumer_redis()
    result = cast(
        tuple[str, list[Any], list[str]],
        redis.xautoclaim(
            name=settings.EVENT_STREAM,
            groupname=settings.CONSUMER_GROUP,
            consumername=consumer_name,
            min_idle_time=settings.CONSUMER_MIN_IDLE_MS,
            start_id=claim_cursor,
            count=settings.CONSUMER_BATCH_SIZE,
        ),
    )
    claim_cursor, messages, deleted_ids = result

    if deleted_ids:
        logger.error(
            "Redis dropped unread events before they were processed: %s", deleted_ids
        )

    for message_id, fields in messages:
        # A worker that crashes mid-event never reaches the except branch in
        # process_message, so the delivery count is checked here as well.
        deliveries = delivery_count(settings.EVENT_STREAM, message_id, redis)
        if deliveries > settings.CONSUMER_MAX_DELIVERIES:
            logger.error(
                "Event %s was delivered %s times, giving up", message_id, deliveries
            )
            send_to_dead_letter(
                settings.EVENT_STREAM,
                message_id,
                fields,
                f"delivered {deliveries} times without being finished",
                redis,
            )
            continue
        process_message(settings.EVENT_STREAM, message_id, fields, redis)


def consume_once(redis: Redis | None = None) -> None:
    redis = redis if redis is not None else get_consumer_redis()
    claim_pending_messages(redis)
    messages = cast(
        list[Any] | None,
        redis.xreadgroup(
            groupname=settings.CONSUMER_GROUP,
            consumername=consumer_name,
            streams={settings.EVENT_STREAM: ">"},
            count=settings.CONSUMER_BATCH_SIZE,
            block=settings.CONSUMER_BLOCK_MS,
        ),
    )
    # stream_name: the Redis Stream key, e.g. events:tasks.
    # entries: the messages read from it.
    if not messages:
        return

    for stream_name, entries in messages:
        for message_id, fields in entries:
            process_message(stream_name, message_id, fields, redis)
