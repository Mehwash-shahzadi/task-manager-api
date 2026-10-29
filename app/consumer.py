from redis import Redis
from redis.exceptions import ResponseError
import logging
import json
import os
import socket


from app.config import settings

logger = logging.getLogger(__name__)

#y milliseconds ko seconds m convert krein ga bcz socket timeout ko
#ko seconds m value chaye
timeout = settings.CONSUMER_BLOCK_MS / 1000 + 3

consumer_redis = Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_timeout=timeout,#Connection banne ke baad Redis ke response ka wait
    socket_connect_timeout=settings.REDIS_TIMEOUT,#redis se connection banane ka wait.
)

#har worker ka apna naam (hostname + process id), taake do workers ke naam alag hon
consumer_name = f"{socket.gethostname()}-{os.getpid()}"


def ensure_group():
    try:
        consumer_redis.xgroup_create(
            name=settings.EVENT_STREAM,
            groupname=settings.CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            pass
        else:
            raise


def handle_event(event):
    logger.info("Received event: %s, %s, %s", event["event_id"], event["event_type"], event["data"])


def send_to_dead_letter(stream_name, message_id, fields, reason):
    dead_stream = settings.EVENT_STREAM + ":dead"

    dead_fields = fields.copy()
    dead_fields["error"] = reason
    dead_fields["original_id"] = message_id

    consumer_redis.xadd(dead_stream, dead_fields)
    consumer_redis.xack(stream_name, settings.CONSUMER_GROUP, message_id)


def process_message(stream_name, message_id, fields):
    try:
        event = json.loads(fields["payload"])
        if not isinstance(event, dict):
            raise TypeError("event must be a dictionary")
        event_id = event["event_id"]
        event_type = event["event_type"]
        if not event_id:
            raise ValueError("event_id cannot be empty")
        if not event_type:
            raise ValueError("event_type cannot be empty")
        if event_type != "task.created":
            raise ValueError("event type must be task.created")
    except (ValueError, KeyError, TypeError) as exc:
        send_to_dead_letter(stream_name, message_id, fields, str(exc))
        return

    try:
        key = "seen:" + event_id
        if consumer_redis.exists(key):
            consumer_redis.xack(stream_name, settings.CONSUMER_GROUP, message_id)
        else:
            handle_event(event)
            consumer_redis.set(key, 1, ex=settings.CONSUMER_SEEN_TTL_SECONDS)
            consumer_redis.xack(stream_name, settings.CONSUMER_GROUP, message_id)
    except Exception as exc:
        logger.exception("Failed to process message %s", message_id)
        pending = consumer_redis.xpending_range(
            stream_name,
            settings.CONSUMER_GROUP,
            message_id,#kahan se search karna hai
            message_id,#kahan tak search karna hai
            1,#maximum kitne results chahiye
        )
        time_delivered = pending[0]["times_delivered"]
        if time_delivered >= settings.CONSUMER_MAX_DELIVERIES:
            send_to_dead_letter(stream_name, message_id, fields, str(exc))


def claim_pending_messages():
    result = consumer_redis.xautoclaim(
        name=settings.EVENT_STREAM,
        groupname=settings.CONSUMER_GROUP,
        consumername=consumer_name,
        min_idle_time=settings.CONSUMER_MIN_IDLE_MS,
        start_id="0-0",
        count=settings.CONSUMER_BATCH_SIZE,
    )
    next_start_id, messages, deleted_ids = result
    for message_id, fields in messages:
        process_message(settings.EVENT_STREAM, message_id, fields)


def consume_once():
    claim_pending_messages()
    messages = consumer_redis.xreadgroup(
        groupname=settings.CONSUMER_GROUP,
        consumername=consumer_name,
        streams={settings.EVENT_STREAM: ">"},
        count=settings.CONSUMER_BATCH_SIZE,
        block=settings.CONSUMER_BLOCK_MS,
    )
    #stream_name — Redis Stream ka naam, jaise events:tasks.
    #entries — us stream se mile hue messages
    if not messages:
        return

    for stream_name, entries in messages:
        for message_id, fields in entries:
            # event = json.loads(fields["payload"])
            # event_id=event["event_id"]
            # key="seen:" + event_id
            # if consumer_redis.exists(key):
            #   consumer_redis.xack(stream_name, settings.CONSUMER_GROUP, message_id)
            # else:
            #  handle_event(event)
            # consumer_redis.set(key, 1, ex=settings.CONSUMER_SEEN_TTL_SECONDS)
            #  consumer_redis.xack(stream_name, settings.CONSUMER_GROUP, message_id)
            process_message(stream_name, message_id, fields)
