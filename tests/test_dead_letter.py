"""The dead-letter stream: its size limit and replay."""

from app.config import settings
from app.enums import EventType
from app.messaging import consumer
from app.messaging.replay import replay_dead

# ---------- L20: dead-letter stream is limited and can be replayed ----------


def test_dead_letter_stream_is_trimmed(monkeypatch):
    seen = {}

    class Pipe:
        def xadd(self, name, fields, **kwargs):
            seen.update(kwargs)

        def xack(self, *args):
            pass

        def execute(self):
            pass

    class R:
        def pipeline(self, transaction=True):
            return Pipe()

    monkeypatch.setattr("app.messaging.consumer.get_consumer_redis", R)
    consumer.send_to_dead_letter("events:tasks", "1-0", {"payload": "x"}, "bad")

    assert seen == {"maxlen": settings.DEAD_STREAM_MAXLEN, "approximate": True}


def test_replay_moves_dead_events_back(stream_redis, monkeypatch):
    monkeypatch.setattr("app.messaging.replay.get_consumer_redis", lambda: stream_redis)
    stream_redis.xadd(
        consumer.DEAD_STREAM,
        {
            "event_type": EventType.TASK_CREATED,
            "payload": "{}",
            "error": "x",
            "original_id": "1-0",
        },
    )

    assert replay_dead() == 1

    assert stream_redis.entries(consumer.DEAD_STREAM) == []
    (_, fields) = stream_redis.entries(settings.EVENT_STREAM)[0]
    assert fields == {
        "event_type": EventType.TASK_CREATED,
        "payload": "{}",
    }  # no error fields


def test_replay_respects_limit(stream_redis, monkeypatch):
    monkeypatch.setattr("app.messaging.replay.get_consumer_redis", lambda: stream_redis)
    for _ in range(3):
        stream_redis.xadd(consumer.DEAD_STREAM, {"event_type": "e", "payload": "{}"})

    assert replay_dead(limit=2) == 2
    assert len(stream_redis.entries(consumer.DEAD_STREAM)) == 1
