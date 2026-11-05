"""Engine and Redis clients are built on first use and can be passed in."""

import threading
from pathlib import Path

from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def add_outbox_row(session):
    from app.enums import EventType
    from app.messaging.publisher import add_to_outbox

    add_to_outbox(session, EventType.TASK_CREATED, {"id": "1"})
    session.commit()


# ---------- E7: engine and Redis clients are built on first use ----------


def test_importing_the_app_builds_no_engine_and_no_redis_client():
    import subprocess
    import sys

    script = (
        "import app.main, app.celery_app, app.celery_jobs, app.overdue, "
        "app.messaging.consumer, app.messaging.outbox, app.messaging.publisher, "
        "app.messaging.monitor\n"
        "from app.database import get_engine\n"
        "from app.redis_client import get_cache_client, get_redis_client\n"
        "from app.messaging.consumer import get_consumer_redis\n"
        "for f in (get_engine, get_redis_client, get_cache_client, get_consumer_redis):\n"
        "    print(f.__name__, f.cache_info().currsize)\n"
    )

    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    sizes = dict(line.split() for line in done.stdout.splitlines())
    assert sizes == {
        "get_engine": "0",
        "get_redis_client": "0",
        "get_cache_client": "0",
        "get_consumer_redis": "0",
    }


def test_each_getter_builds_once_and_then_returns_the_same_object():
    from app.database import get_engine
    from app.messaging.consumer import get_consumer_redis
    from app.redis_client import get_cache_client, get_redis_client

    for getter in (get_engine, get_redis_client, get_cache_client, get_consumer_redis):
        assert getter() is getter()


def test_the_getters_build_the_right_kind_of_object():
    from app.database import get_engine
    from app.messaging.consumer import get_consumer_redis
    from app.redis_client import get_cache_client, get_redis_client

    assert get_engine().url.database == "tasks"
    assert get_redis_client() is not get_cache_client()
    assert get_redis_client() is not get_consumer_redis()
    pool = get_engine().pool
    assert pool.size() == 10  # type: ignore[attr-defined]


# ---------- E2: the Redis getters live in app/redis_client.py ----------


def test_messaging_no_longer_imports_the_cache_module():
    import ast

    for name in ("outbox.py", "publisher.py", "monitor.py"):
        tree = ast.parse((PROJECT_ROOT / "app" / "messaging" / name).read_text())
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "app.cache" not in modules, name
    for name in ("outbox.py", "monitor.py"):  # The two that talk to Redis.
        tree = ast.parse((PROJECT_ROOT / "app" / "messaging" / name).read_text())
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "app.redis_client" in modules, name


def test_the_cache_module_uses_the_same_getters():
    from app import cache, redis_client

    assert cache.get_cache_client is redis_client.get_cache_client


# ---------- I4/I5: the engine and Redis can be passed in ----------


def test_relay_works_with_an_engine_and_redis_passed_in(session, test_engine):
    from app.messaging.outbox import cleanup_sent, relay_once
    from tests.fakes import FakeRedis

    add_outbox_row(session)
    redis = FakeRedis()

    assert relay_once(test_engine, redis) == 1  # No monkeypatch anywhere.
    assert len(redis.calls) == 1
    assert cleanup_sent(test_engine) == 0  # Nothing old enough to delete.


def test_events_helpers_work_with_passed_in_objects(session, test_engine):
    from app.messaging.monitor import pending_count, stream_backlog
    from tests.fakes import GroupsRedis

    add_outbox_row(session)

    assert pending_count(test_engine) == 1
    group = {"name": settings.CONSUMER_GROUP, "lag": 4, "pending": 1}
    assert stream_backlog(GroupsRedis([group])) == 5


def test_consumer_works_with_a_redis_passed_in(monkeypatch):
    import json

    from app.messaging import consumer
    from tests.fakes import FakeStreamRedis

    def must_not_be_used():
        raise AssertionError("the default client must not be built")

    monkeypatch.setattr(consumer, "get_consumer_redis", must_not_be_used)
    handled = []
    monkeypatch.setattr(consumer, "handle_event", handled.append)
    redis = FakeStreamRedis()
    consumer.ensure_group(redis)
    redis.xadd(
        settings.EVENT_STREAM,
        {"payload": json.dumps({"event_id": "e1", "event_type": "task.created"})},
    )

    consumer.consume_once(redis)

    assert [e.event_id for e in handled] == ["e1"]
    assert redis.pending_ids(settings.EVENT_STREAM, settings.CONSUMER_GROUP) == []


def test_dead_letter_and_replay_work_with_a_redis_passed_in():
    from app.messaging import consumer
    from app.messaging.replay import replay_dead
    from tests.fakes import FakeStreamRedis

    redis = FakeStreamRedis()
    consumer.ensure_group(redis)
    fields = {"event_type": "task.created", "payload": "{}"}

    consumer.send_to_dead_letter(settings.EVENT_STREAM, "1-0", fields, "bad", redis)
    assert len(redis.entries(consumer.DEAD_STREAM)) == 1

    assert replay_dead(redis=redis) == 1
    assert redis.entries(consumer.DEAD_STREAM) == []
    assert len(redis.entries(settings.EVENT_STREAM)) == 1


# ---------- R-20: the worker takes its Redis as a parameter ----------


def test_the_worker_runs_with_the_redis_it_is_given(monkeypatch):

    from app.messaging import worker
    from tests.fakes import FakeStreamRedis

    fake = FakeStreamRedis()
    stop = threading.Event()
    seen = []

    def consume_once(redis):
        seen.append(redis)
        stop.set()

    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "consume_once", consume_once)

    worker.main(stop, fake)

    assert seen == [fake]
    assert settings.EVENT_STREAM in fake.streams  # ensure_group made the stream.


def test_there_is_one_way_to_get_a_client_and_no_container():
    assert not (PROJECT_ROOT / "app" / "container.py").exists()
