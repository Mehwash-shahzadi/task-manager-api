import threading

import pytest
from redis import ConnectionError as RedisConnectionError

from app.messaging import worker


@pytest.fixture(autouse=True)
def clean_tables():
    """These tests do not touch the database, so skip the conftest cleanup."""
    yield


@pytest.fixture(autouse=True)
def worker_env(monkeypatch):
    monkeypatch.setattr(worker, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "ensure_group", lambda *args: None)


def run_with(monkeypatch, steps):
    """consume_once runs each step in turn, then asks the worker to stop."""
    steps = list(steps)
    calls = []
    stop = threading.Event()  # Each run gets its own flag.

    def fake_consume_once(*args):
        calls.append(1)
        if not steps:
            stop.set()
            return
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step

    monkeypatch.setattr(worker, "consume_once", fake_consume_once)
    worker.main(stop)
    return len(calls)


def test_request_stop_sets_the_given_flag():
    stop = threading.Event()

    worker.request_stop(stop, 15)

    assert stop.is_set()


def test_sigterm_stops_loop_after_current_batch(monkeypatch):
    handlers = {}
    monkeypatch.setattr(
        worker.signal,
        "signal",
        lambda signum, handler: handlers.update({signum: handler}),
    )
    calls = []

    def consume_once(*args):
        calls.append(1)
        handlers[worker.signal.SIGTERM](15, None)  # The signal arrives mid-batch.

    monkeypatch.setattr(worker, "consume_once", consume_once)
    worker.main()
    assert calls == [1]


def test_two_workers_do_not_share_a_stop_flag(monkeypatch):
    first, second = threading.Event(), threading.Event()

    worker.request_stop(first, 15)

    assert first.is_set()
    assert not second.is_set()
    assert not hasattr(worker, "stop")  # No global flag any more.


def test_redis_down_is_retried_then_continues(monkeypatch):
    errors = [RedisConnectionError("down"), RedisConnectionError("down")]
    assert (
        run_with(monkeypatch, [*errors, None]) == 4
    )  # 2 failures, 1 ok, 1 stopping call


def test_unexpected_error_does_not_stop_loop(monkeypatch):
    assert run_with(monkeypatch, [RuntimeError("bug"), None]) == 3


def test_group_is_created_once_and_again_after_a_redis_error(monkeypatch):
    created = []
    monkeypatch.setattr(worker, "ensure_group", lambda *args: created.append(1))

    # ok, ok, redis error, ok, then the stopping call
    run_with(monkeypatch, [None, None, RedisConnectionError("down"), None])

    assert len(created) == 2  # at start, and once more after the error
