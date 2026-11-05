import json
import logging
from pathlib import Path

import pytest
from sqlmodel import select

from app.config import settings
from app.enums import EventType
from app.messaging import consumer
from app.messaging.consumer import (
    claim_pending_messages,
    consume_once,
    ensure_group,
    process_message,
)
from app.models.outbox import OutboxEvent

STREAM = settings.EVENT_STREAM
GROUP = settings.CONSUMER_GROUP
DEAD = STREAM + ":dead"


def make_fields(event_id="e1", event_type=EventType.TASK_CREATED, title="Buy milk"):
    envelope = {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": "2026-01-01T00:00:00Z",
        "source": "task-manager-api",
        "data": {"id": 7, "title": title},
    }
    return {"event_type": event_type, "payload": json.dumps(envelope)}


@pytest.fixture(autouse=True)
def clean_tables():
    """These tests do not touch the database, so skip the conftest cleanup."""
    yield


@pytest.fixture
def handled(monkeypatch):
    """Replaces handle_event with a recorder."""
    calls = []
    monkeypatch.setattr(consumer, "handle_event", calls.append)
    return calls


@pytest.fixture
def failing_handler(monkeypatch):
    def boom(event):
        raise RuntimeError("handler failed")

    monkeypatch.setattr(consumer, "handle_event", boom)


# task 1: settings and client


def test_consumer_client_timeout_is_longer_than_block_time():
    assert consumer.get_consumer_redis().connection_pool.connection_kwargs[
        "socket_timeout"
    ] > (settings.CONSUMER_BLOCK_MS / 1000)


def test_shared_client_is_not_the_consumer_client():
    from app.redis_client import get_redis_client

    assert get_redis_client() is not consumer.get_consumer_redis()


# task 2: the fake itself


def test_fake_raises_busygroup_on_second_create(stream_redis):
    stream_redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    with pytest.raises(consumer.ResponseError, match="BUSYGROUP"):
        stream_redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)


def test_fake_read_but_not_acked_is_pending(stream_redis):
    stream_redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    entry_id = stream_redis.xadd(STREAM, make_fields())
    stream_redis.xreadgroup(GROUP, "c1", {STREAM: ">"})
    assert stream_redis.pending_ids(STREAM, GROUP) == [entry_id]
    stream_redis.xack(STREAM, GROUP, entry_id)
    assert stream_redis.pending_ids(STREAM, GROUP) == []


def test_fake_set_nx_does_not_overwrite(stream_redis):
    assert stream_redis.set("k", 1, nx=True, ex=10) is True
    assert stream_redis.set("k", 2, nx=True, ex=10) is None


# task 3: group and consume


def test_ensure_group_twice_does_not_fail(stream_redis):
    ensure_group()
    ensure_group()


def test_event_is_handled_and_acked(stream_redis, handled):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields())
    consume_once()
    assert len(handled) == 1
    assert handled[0].event_id == "e1"
    assert handled[0].data["title"] == "Buy milk"
    assert stream_redis.pending_ids(STREAM, GROUP) == []


def test_group_starts_at_zero_so_old_entries_are_read(stream_redis, handled):
    stream_redis.xadd(STREAM, make_fields("old"))
    ensure_group()
    consume_once()
    assert [e.event_id for e in handled] == ["old"]


def test_empty_stream_returns_without_error(stream_redis, handled):
    ensure_group()
    consume_once()
    assert handled == []


def test_log_line_has_event_id_and_type_but_not_the_task_text(stream_redis, caplog):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields())
    with caplog.at_level(logging.INFO, logger="app.messaging.consumer"):
        consume_once()
    text = caplog.text
    assert "e1" in text
    assert EventType.TASK_CREATED in text
    assert "Buy milk" not in text


def test_event_id_that_is_not_a_string_goes_straight_to_dead_stream(
    stream_redis, handled
):
    ensure_group()
    fields = make_fields(event_id=123)
    entry_id = stream_redis.xadd(STREAM, fields)
    stream_redis.xreadgroup(GROUP, "c1", {STREAM: ">"})
    process_message(STREAM, entry_id, fields)
    assert handled == []
    dead = stream_redis.entries(DEAD)
    assert len(dead) == 1
    assert "event_id" in dead[0][1]["error"]


# task 4: duplicates


def test_duplicate_event_is_handled_once_and_both_acked(stream_redis, handled):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields("dup"))
    stream_redis.xadd(STREAM, make_fields("dup"))
    consume_once()
    assert len(handled) == 1
    assert stream_redis.pending_ids(STREAM, GROUP) == []


def test_seen_key_has_ttl(stream_redis, handled):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields("e1"))
    consume_once()
    assert (
        stream_redis.ttl(consumer.seen_key("e1")) == settings.CONSUMER_SEEN_TTL_SECONDS
    )


def test_seen_key_contains_the_group_name():
    assert (
        consumer.seen_key("e1")
        == f"{settings.KEY_PREFIX}seen:{settings.CONSUMER_GROUP}:e1"
    )


def test_claim_is_short_while_the_handler_runs(stream_redis, monkeypatch):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields("e1"))
    seen_ttls = []

    def spy(event):
        seen_ttls.append(stream_redis.ttl(consumer.seen_key("e1")))

    monkeypatch.setattr(consumer, "handle_event", spy)
    consume_once()

    assert seen_ttls == [max(settings.CONSUMER_MIN_IDLE_MS // 1000, 1)]
    assert (
        stream_redis.ttl(consumer.seen_key("e1")) == settings.CONSUMER_SEEN_TTL_SECONDS
    )


def test_event_already_done_is_not_handled_again_and_is_acknowledged(
    stream_redis, handled
):
    ensure_group()
    entry_id = stream_redis.xadd(STREAM, make_fields("e1"))
    stream_redis.set(consumer.seen_key("e1"), consumer.SEEN_DONE, ex=30)

    consume_once()

    assert handled == []
    assert entry_id not in stream_redis.pending_ids(STREAM, GROUP)


def test_event_another_worker_is_still_handling_stays_pending(stream_redis, handled):
    ensure_group()
    entry_id = stream_redis.xadd(STREAM, make_fields("e1"))
    stream_redis.set(consumer.seen_key("e1"), consumer.SEEN_PROCESSING, ex=30)

    consume_once()

    assert handled == []  # Not handled twice...
    assert entry_id in stream_redis.pending_ids(STREAM, GROUP)  # ...and not lost.


def test_event_comes_back_and_is_handled_if_the_other_worker_failed(
    stream_redis, handled
):
    ensure_group()
    entry_id = stream_redis.xadd(STREAM, make_fields("e1"))
    key = consumer.seen_key("e1")
    stream_redis.set(key, consumer.SEEN_PROCESSING, ex=30)
    consume_once()  # Worker B backs off: the event stays pending.
    stream_redis.delete(key)  # Worker A failed: its claim is removed.
    stream_redis.advance(settings.CONSUMER_MIN_IDLE_MS + 1)

    consume_once()  # The event is claimed again and handled.

    assert [e.event_id for e in handled] == ["e1"]
    assert entry_id not in stream_redis.pending_ids(STREAM, GROUP)
    assert stream_redis.get(key) == consumer.SEEN_DONE


def test_a_handled_event_is_marked_done_for_the_full_window(stream_redis, handled):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields("e1"))

    consume_once()

    key = consumer.seen_key("e1")
    assert stream_redis.get(key) == consumer.SEEN_DONE
    assert stream_redis.ttl(key) == settings.CONSUMER_SEEN_TTL_SECONDS


def test_handler_error_does_not_record_event_id(stream_redis, failing_handler):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields("e1"))
    consume_once()
    assert stream_redis.exists(consumer.seen_key("e1")) == 0


# task 5: retry and reclaim


def test_failing_handler_leaves_entry_pending(stream_redis, failing_handler):
    ensure_group()
    entry_id = stream_redis.xadd(STREAM, make_fields())
    consume_once()
    assert stream_redis.pending_ids(STREAM, GROUP) == [entry_id]
    assert stream_redis.entries(DEAD) == []


def test_idle_entry_is_reclaimed_by_another_consumer(
    stream_redis, handled, monkeypatch
):
    ensure_group()
    entry_id = stream_redis.xadd(STREAM, make_fields())
    # a consumer that crashed: it read the entry and never acked
    stream_redis.xreadgroup(GROUP, "crashed-worker", {STREAM: ">"})

    monkeypatch.setattr(consumer, "consumer_name", "other-worker")
    claim_pending_messages()
    assert handled == []  # not idle long enough yet

    stream_redis.advance(settings.CONSUMER_MIN_IDLE_MS)
    claim_pending_messages()
    assert len(handled) == 1
    assert stream_redis.pending_ids(STREAM, GROUP) == []
    assert entry_id not in stream_redis.pending_ids(STREAM, GROUP)


def test_retry_that_succeeds_is_acked(stream_redis, monkeypatch):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields())
    state = {"fail": True, "calls": 0}

    def flaky(event):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("first time fails")

    monkeypatch.setattr(consumer, "handle_event", flaky)
    consume_once()
    assert len(stream_redis.pending_ids(STREAM, GROUP)) == 1

    state["fail"] = False
    stream_redis.advance(settings.CONSUMER_MIN_IDLE_MS)
    consume_once()
    assert state["calls"] == 2
    assert stream_redis.pending_ids(STREAM, GROUP) == []


def test_one_bad_event_does_not_stop_the_batch(stream_redis, monkeypatch):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields("bad"))
    stream_redis.xadd(STREAM, make_fields("good"))
    done = []

    def handler(event):
        if event.event_id == "bad":
            raise RuntimeError("boom")
        done.append(event.event_id)

    monkeypatch.setattr(consumer, "handle_event", handler)
    consume_once()
    assert done == ["good"]
    assert len(stream_redis.pending_ids(STREAM, GROUP)) == 1


# task 6: dead letter


@pytest.mark.parametrize(
    "fields",
    [
        {"payload": "{not json"},
        {"event_type": EventType.TASK_CREATED},  # no payload field
        {"payload": json.dumps([1, 2])},  # not a dict
        {"payload": json.dumps({"event_type": EventType.TASK_CREATED})},  # no event_id
        {"payload": json.dumps({"event_id": "e1"})},  # no event_type
        {"payload": json.dumps({"event_id": "", "event_type": EventType.TASK_CREATED})},
        {"payload": json.dumps({"event_id": "e1", "event_type": "task.deleted"})},
    ],
)
def test_poison_entry_goes_to_dead_stream(stream_redis, handled, fields):
    ensure_group()
    entry_id = stream_redis.xadd(STREAM, fields)
    consume_once()
    assert handled == []
    assert stream_redis.pending_ids(STREAM, GROUP) == []
    dead = stream_redis.entries(DEAD)
    assert len(dead) == 1
    dead_fields = dead[0][1]
    assert dead_fields["error"]
    assert dead_fields["original_id"] == entry_id
    for key, value in fields.items():
        assert dead_fields[key] == value


def test_entry_failing_max_deliveries_goes_to_dead_stream(
    stream_redis, failing_handler
):
    ensure_group()
    fields = make_fields()
    entry_id = stream_redis.xadd(STREAM, fields)
    for _ in range(settings.CONSUMER_MAX_DELIVERIES):
        assert stream_redis.entries(DEAD) == []
        consume_once()
        stream_redis.advance(settings.CONSUMER_MIN_IDLE_MS)

    dead = stream_redis.entries(DEAD)
    assert len(dead) == 1
    assert dead[0][1]["error"] == "handler failed"
    assert dead[0][1]["original_id"] == entry_id
    assert dead[0][1]["payload"] == fields["payload"]
    assert stream_redis.pending_ids(STREAM, GROUP) == []


def test_process_message_directly_with_bad_json(stream_redis, handled):
    ensure_group()
    entry_id = stream_redis.xadd(STREAM, {"payload": "nope"})
    stream_redis.xreadgroup(GROUP, "c1", {STREAM: ">"})
    process_message(STREAM, entry_id, {"payload": "nope"})
    assert len(stream_redis.entries(DEAD)) == 1


def test_dead_letter_move_is_one_transaction(stream_redis, monkeypatch):
    ensure_group()
    entry_id = stream_redis.xadd(STREAM, make_fields())
    stream_redis.xreadgroup(GROUP, consumer.consumer_name, {STREAM: ">"})

    pipe = stream_redis.pipeline(transaction=True)

    def crash():
        raise RuntimeError("process died before EXEC")

    monkeypatch.setattr(pipe, "execute", crash)
    monkeypatch.setattr(stream_redis, "pipeline", lambda transaction=True: pipe)

    with pytest.raises(RuntimeError):
        consumer.send_to_dead_letter(STREAM, entry_id, make_fields(), "bad")

    # nothing was applied: not in the dead stream, still pending in the main stream
    assert stream_redis.entries(DEAD) == []
    assert stream_redis.pending_ids(STREAM, GROUP) == [entry_id]


def test_claim_scan_continues_from_the_returned_cursor(stream_redis, monkeypatch):
    monkeypatch.setattr(consumer, "claim_cursor", "0-0")
    seen_start_ids = []
    replies = iter([("5-0", [], []), ("0-0", [], []), ("0-0", [], [])])

    def fake_xautoclaim(**kwargs):
        seen_start_ids.append(kwargs["start_id"])
        return next(replies)

    monkeypatch.setattr(stream_redis, "xautoclaim", fake_xautoclaim)

    claim_pending_messages()  # starts at the beginning, Redis says "continue at 5-0"
    claim_pending_messages()  # continues at 5-0, Redis says the scan is finished
    claim_pending_messages()  # starts over

    assert seen_start_ids == ["0-0", "5-0", "0-0"]


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _event_fields(event_id="e1"):
    import json

    payload = {"event_id": event_id, "event_type": "task.created"}
    return {"payload": json.dumps(payload)}


# ---------- C1/C2/C4: one EventType and one TaskEvent model ----------


def test_event_type_is_written_in_one_place():
    from app.enums import EventType

    assert EventType.TASK_CREATED == "task.created"
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        if path.name == "enums.py":
            continue
        assert '"task.created"' not in path.read_text(), path


def test_task_event_accepts_a_good_event():
    from app.schemas.event import TaskEvent

    event = TaskEvent.model_validate_json(
        '{"event_id": "e1", "event_type": "task.created", "data": {"a": 1}}'
    )

    assert event.event_type == "task.created"
    assert event.version == 1
    assert event.data == {"a": 1}


@pytest.mark.parametrize(
    "body",
    [
        {"event_type": "task.created"},  # No id.
        {"event_id": "", "event_type": "task.created"},  # Empty id.
        {"event_id": 123, "event_type": "task.created"},  # Id is not text.
        {"event_id": "e1"},  # No type.
        {"event_id": "e1", "event_type": "task.deleted"},  # Unknown type.
        {
            "event_id": "e1",
            "event_type": "task.created",
            "data": [1],
        },  # Data not a dict.
    ],
)
def test_task_event_rejects_a_bad_event(body):
    from pydantic import ValidationError

    from app.schemas.event import TaskEvent

    with pytest.raises(ValidationError):
        TaskEvent.model_validate(body)


def test_the_outbox_payload_is_a_valid_task_event(session):
    from app.enums import EventType
    from app.messaging.publisher import add_to_outbox
    from app.schemas.event import TaskEvent

    add_to_outbox(session, EventType.TASK_CREATED, {"id": "1"})
    session.commit()

    row = session.exec(select(OutboxEvent)).one()
    event = TaskEvent.model_validate(row.payload)  # The consumer would accept it.
    assert event.event_id == str(row.id)
    assert event.source == "task-manager-api"
    assert event.occurred_at is not None
    assert row.event_type == "task.created"


def test_a_bad_event_goes_to_the_dead_letter_stream_with_a_readable_reason(
    stream_redis,
):
    import json

    from app.messaging import consumer

    consumer.ensure_group()
    fields = {"payload": json.dumps({"event_id": "e1", "event_type": "nope"})}
    entry_id = stream_redis.xadd(settings.EVENT_STREAM, fields)
    stream_redis.xreadgroup(settings.CONSUMER_GROUP, "c1", {settings.EVENT_STREAM: ">"})

    consumer.process_message(settings.EVENT_STREAM, entry_id, fields)

    dead = stream_redis.entries(consumer.DEAD_STREAM)
    assert len(dead) == 1
    assert "event_type" in dead[0][1]["error"]


# ---------- L17: an event that keeps crashing the worker ends in the dead-letter stream ----------


def test_an_event_that_crashes_the_worker_every_time_is_dead_lettered(
    stream_redis, monkeypatch
):
    import json

    from app.messaging import consumer

    consumer.ensure_group()
    fields = {"payload": json.dumps({"event_id": "boom", "event_type": "task.created"})}
    stream_redis.xadd(settings.EVENT_STREAM, fields)
    stream_redis.xreadgroup(settings.CONSUMER_GROUP, "c1", {settings.EVENT_STREAM: ">"})
    started = []
    # The worker "crashes": it starts the event but never acks it.
    monkeypatch.setattr(consumer, "process_message", lambda *args: started.append(1))

    for _ in range(settings.CONSUMER_MAX_DELIVERIES + 2):
        stream_redis.advance(settings.CONSUMER_MIN_IDLE_MS + 1)
        consumer.claim_pending_messages()

    # Delivery 1 was the first read. The handler may start in deliveries 2 to MAX.
    assert len(started) == settings.CONSUMER_MAX_DELIVERIES - 1
    dead = stream_redis.entries(consumer.DEAD_STREAM)
    assert len(dead) == 1
    assert "without being finished" in dead[0][1]["error"]
    assert (
        stream_redis.pending_ids(settings.EVENT_STREAM, settings.CONSUMER_GROUP) == []
    )


def test_delivery_count_is_zero_for_a_message_that_is_not_pending(stream_redis):
    from app.messaging import consumer

    consumer.ensure_group()

    assert consumer.delivery_count(settings.EVENT_STREAM, "1-0") == 0


# ---------- L15: repeats are caught for CONSUMER_SEEN_TTL_SECONDS, not longer ----------


def test_a_repeat_is_ignored_inside_the_window_and_handled_again_after_it(monkeypatch):
    import json

    from app.messaging import consumer
    from tests.fakes import FakeStreamRedis

    handled = []
    monkeypatch.setattr(consumer, "handle_event", handled.append)
    redis = FakeStreamRedis()
    consumer.ensure_group(redis)
    fields = {"payload": json.dumps({"event_id": "e1", "event_type": "task.created"})}
    key = consumer.seen_key("e1")

    consumer.process_message(settings.EVENT_STREAM, "1-0", fields, redis)
    assert redis.ttl(key) == settings.CONSUMER_SEEN_TTL_SECONDS  # The window.

    consumer.process_message(settings.EVENT_STREAM, "2-0", fields, redis)
    assert len(handled) == 1  # Inside the window: ignored.

    redis.keys.pop(key)  # What Redis does when the window ends.
    consumer.process_message(settings.EVENT_STREAM, "3-0", fields, redis)
    assert len(handled) == 2  # After the window: handled again.


def test_the_readme_explains_the_repeat_window():
    text = (PROJECT_ROOT / "README.md").read_text()

    assert "CONSUMER_SEEN_TTL_SECONDS" in text
    assert "handlers should be safe to run twice" in text


# ---------- I6: a handler for each event type ----------


def test_the_task_created_handler_is_registered_by_default():
    from app.enums import EventType
    from app.messaging import consumer

    assert EventType.TASK_CREATED in consumer.HANDLERS


def test_handle_event_calls_the_handler_registered_for_the_type(monkeypatch):
    from app.enums import EventType
    from app.messaging import consumer
    from tests.fakes import FakeStreamRedis

    got = []
    monkeypatch.setitem(consumer.HANDLERS, EventType.TASK_CREATED, got.append)
    redis = FakeStreamRedis()
    consumer.ensure_group(redis)

    consumer.process_message(settings.EVENT_STREAM, "1-0", _event_fields(), redis)

    assert [e.event_id for e in got] == ["e1"]


def test_register_handler_replaces_the_handler(monkeypatch):
    from app.enums import EventType
    from app.messaging import consumer

    monkeypatch.setattr(consumer, "HANDLERS", dict(consumer.HANDLERS))
    calls = []

    consumer.register_handler(EventType.TASK_CREATED, calls.append)

    assert consumer.HANDLERS[EventType.TASK_CREATED] == calls.append


def test_an_event_type_without_a_handler_goes_straight_to_the_dead_letter_stream(
    monkeypatch,
):
    from app.enums import EventType
    from app.messaging import consumer
    from tests.fakes import FakeStreamRedis

    monkeypatch.delitem(consumer.HANDLERS, EventType.TASK_CREATED)
    redis = FakeStreamRedis()
    consumer.ensure_group(redis)

    consumer.process_message(settings.EVENT_STREAM, "1-0", _event_fields(), redis)

    dead = redis.entries(consumer.DEAD_STREAM)
    assert len(dead) == 1
    assert "no handler" in dead[0][1]["error"]
    assert redis.keys == {}  # Nothing was claimed, so no repeat marker either.
