import pytest
from redis import ConnectionError as RedisConnectionError

import app.worker as worker


@pytest.fixture(autouse=True)
def clean_tables():
    """These tests do not touch the database, so skip the conftest cleanup."""
    yield


@pytest.fixture(autouse=True)
def worker_env(monkeypatch):
    worker.stop.clear()
    monkeypatch.setattr(worker, "RETRY_WAIT_SECONDS", 0)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "ensure_group", lambda: None)
    yield
    worker.stop.clear()


def run_with(monkeypatch, steps):
    """consume_once runs each step in turn, then asks the worker to stop."""
    steps = list(steps)
    calls = []

    def fake_consume_once():
        calls.append(1)
        if not steps:
            worker.stop.set()
            return
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step

    monkeypatch.setattr(worker, "consume_once", fake_consume_once)
    worker.main()
    return len(calls)


def test_shutdown_signal_sets_stop():
    worker.handle_shutdown(15, None)
    assert worker.stop.is_set()


def test_sigterm_stops_loop_after_current_batch(monkeypatch):
    calls = []

    def consume_once():
        calls.append(1)
        worker.handle_shutdown(15, None)

    monkeypatch.setattr(worker, "consume_once", consume_once)
    worker.main()
    assert calls == [1]


def test_redis_down_is_retried_then_continues(monkeypatch):
    errors = [RedisConnectionError("down"), RedisConnectionError("down")]
    assert run_with(monkeypatch, errors + [None]) == 4  # 2 failures, 1 ok, 1 stopping call


def test_unexpected_error_does_not_stop_loop(monkeypatch):
    assert run_with(monkeypatch, [RuntimeError("bug"), None]) == 3
