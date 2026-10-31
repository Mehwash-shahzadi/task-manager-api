import json
import logging

import pytest

import app.consumer as consumer
from app.config import settings
from app.consumer import (
    claim_pending_messages,
    consume_once,
    ensure_group,
    process_message,
)
from tests.conftest import FakeStreamRedis

STREAM = settings.EVENT_STREAM
GROUP = settings.CONSUMER_GROUP
DEAD = STREAM + ":dead"


def make_fields(event_id="e1", event_type="task.created", title="Buy milk"):
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
    assert consumer.consumer_redis.connection_pool.connection_kwargs["socket_timeout"] > (
        settings.CONSUMER_BLOCK_MS / 1000
    )


def test_shared_client_is_not_the_consumer_client():
    from app.cache import redis_client

    assert redis_client is not consumer.consumer_redis


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
    assert handled[0]["event_id"] == "e1"
    assert handled[0]["data"]["title"] == "Buy milk"
    assert stream_redis.pending_ids(STREAM, GROUP) == []


def test_group_starts_at_zero_so_old_entries_are_read(stream_redis, handled):
    stream_redis.xadd(STREAM, make_fields("old"))
    ensure_group()
    consume_once()
    assert [e["event_id"] for e in handled] == ["old"]


def test_empty_stream_returns_without_error(stream_redis, handled):
    ensure_group()
    consume_once()
    assert handled == []


def test_log_line_has_event_id_type_task_id_and_title(stream_redis, caplog):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields())
    with caplog.at_level(logging.INFO, logger="app.consumer"):
        consume_once()
    text = caplog.text
    assert "e1" in text
    assert "task.created" in text
    assert "Buy milk" in text
    assert "7" in text


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
    assert stream_redis.ttl("seen:e1") == settings.CONSUMER_SEEN_TTL_SECONDS


def test_handler_error_does_not_record_event_id(stream_redis, failing_handler):
    ensure_group()
    stream_redis.xadd(STREAM, make_fields("e1"))
    consume_once()
    assert stream_redis.exists("seen:e1") == 0


# task 5: retry and reclaim

def test_failing_handler_leaves_entry_pending(stream_redis, failing_handler):
    ensure_group()
    entry_id = stream_redis.xadd(STREAM, make_fields())
    consume_once()
    assert stream_redis.pending_ids(STREAM, GROUP) == [entry_id]
    assert stream_redis.entries(DEAD) == []


def test_idle_entry_is_reclaimed_by_another_consumer(stream_redis, handled, monkeypatch):
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
        if event["event_id"] == "bad":
            raise RuntimeError("boom")
        done.append(event["event_id"])

    monkeypatch.setattr(consumer, "handle_event", handler)
    consume_once()
    assert done == ["good"]
    assert len(stream_redis.pending_ids(STREAM, GROUP)) == 1


# task 6: dead letter

@pytest.mark.parametrize(
    "fields",
    [
        {"payload": "{not json"},
        {"event_type": "task.created"},  # no payload field
        {"payload": json.dumps([1, 2])},  # not a dict
        {"payload": json.dumps({"event_type": "task.created"})},  # no event_id
        {"payload": json.dumps({"event_id": "e1"})},  # no event_type
        {"payload": json.dumps({"event_id": "", "event_type": "task.created"})},
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


def test_entry_failing_max_deliveries_goes_to_dead_stream(stream_redis, failing_handler):
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
