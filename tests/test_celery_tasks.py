import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from redis import ConnectionError as RedisConnectionError
from sqlmodel import select

from app.celery_app import celery_app, dispose_engine_after_fork
from app.celery_jobs import cleanup_outbox, ping, relay_outbox
from app.config import settings
from app.enums import EventType
from app.models.base import current_time
from app.models.outbox import OutboxEvent, OutboxStatus

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------- Task 2: Celery app and the ping job ----------


def test_ping_returns_pong():
    assert ping() == "pong"


def test_ping_runs_through_celery_machinery():
    result = ping.apply()

    assert result.successful()
    assert result.result == "pong"


def test_jobs_are_registered_under_their_full_names():
    assert "app.celery_jobs.ping" in celery_app.tasks
    assert "app.celery_jobs.cleanup_outbox" in celery_app.tasks


def test_app_uses_the_urls_from_settings():
    assert celery_app.conf.broker_url == settings.CELERY_BROKER_URL
    assert celery_app.conf.result_backend == settings.CELERY_RESULT_BACKEND


def test_serializers_are_json_only():
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.result_serializer == "json"
    assert list(celery_app.conf.accept_content) == ["json"]


def test_times_are_in_utc():
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.enable_utc is True


def test_jobs_are_acked_late_and_taken_one_at_a_time():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_track_started is True


def test_results_expire_after_one_hour():
    assert celery_app.conf.result_expires == 3600


def test_worker_retries_broker_connection_on_startup():
    assert celery_app.conf.broker_connection_retry_on_startup is True


def test_importing_the_app_does_not_connect_to_redis():
    # broker and backend point at a closed port: the import must still work
    env = {
        **os.environ,
        "CELERY_BROKER_URL": "redis://127.0.0.1:1/1",
        "CELERY_RESULT_BACKEND": "redis://127.0.0.1:1/2",
    }

    done = subprocess.run(
        [sys.executable, "-c", "import app.celery_app"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert done.returncode == 0, done.stderr


def test_fork_handler_gives_each_child_its_own_connections(monkeypatch):
    calls = []

    class FakeEngine:
        def dispose(self, close=True):
            calls.append(close)

    monkeypatch.setattr("app.celery_app.get_engine", FakeEngine)

    dispose_engine_after_fork()

    # close=False: forget the parent's connections without closing them for the parent
    assert calls == [False]


def test_fork_handler_is_connected_to_the_worker_signal():
    from celery.signals import worker_process_init

    receivers = [ref() for _, ref in worker_process_init.receivers]

    assert dispose_engine_after_fork in receivers


# ---------- Task 4: cleanup_outbox ----------


def add_row(session, name, status, sent_at=None, age_days=0) -> OutboxEvent:
    created = current_time() - timedelta(days=age_days)
    row = OutboxEvent(event_type=EventType.TASK_CREATED, payload={"event_id": name})
    row.status, row.sent_at, row.created_at = status, sent_at, created
    session.add(row)
    session.commit()
    return row


def old_time():
    return current_time() - timedelta(days=settings.OUTBOX_RETENTION_DAYS + 1)


def test_cleanup_outbox_deletes_old_sent_rows_and_returns_the_count(
    session, fake_redis
):
    add_row(session, "old1", OutboxStatus.SENT, sent_at=old_time(), age_days=10)
    add_row(session, "old2", OutboxStatus.SENT, sent_at=old_time(), age_days=10)

    assert cleanup_outbox() == 2

    session.expire_all()
    assert session.exec(select(OutboxEvent)).all() == []


def test_cleanup_outbox_keeps_everything_that_must_stay(session, fake_redis):
    add_row(session, "new_sent", OutboxStatus.SENT, sent_at=current_time())
    add_row(session, "old_pending", OutboxStatus.PENDING, age_days=10)
    add_row(session, "old_dead", OutboxStatus.DEAD, age_days=10)

    assert cleanup_outbox() == 0

    session.expire_all()
    left = {r.payload["event_id"] for r in session.exec(select(OutboxEvent)).all()}
    assert left == {"new_sent", "old_pending", "old_dead"}


def test_cleanup_outbox_works_on_an_empty_table(fake_redis):
    assert cleanup_outbox() == 0


def test_cleanup_outbox_runs_through_celery_machinery(session, fake_redis):
    add_row(session, "old", OutboxStatus.SENT, sent_at=old_time(), age_days=10)

    result = cleanup_outbox.apply()

    assert result.successful()
    assert result.result == 1


def test_cleanup_outbox_logs_how_many_rows_it_deleted(session, fake_redis, caplog):
    add_row(session, "old", OutboxStatus.SENT, sent_at=old_time(), age_days=10)

    with caplog.at_level(logging.INFO, logger="app.celery_jobs"):
        cleanup_outbox()

    assert "Outbox cleanup deleted 1 rows" in caplog.text


def test_cleanup_outbox_logs_and_reraises_errors(monkeypatch, caplog):
    def broken():
        raise RuntimeError("db is down")

    monkeypatch.setattr("app.celery_jobs.cleanup_sent", broken)

    with (
        caplog.at_level(logging.ERROR, logger="app.celery_jobs"),
        pytest.raises(RuntimeError, match="db is down"),
    ):
        cleanup_outbox()

    assert "Outbox cleanup failed" in caplog.text


def test_failed_cleanup_shows_up_as_a_failed_job(monkeypatch):
    def broken():
        raise RuntimeError("db is down")

    monkeypatch.setattr("app.celery_jobs.cleanup_sent", broken)

    result = cleanup_outbox.apply()

    assert result.failed()


def test_cleanup_is_scheduled_every_hour():
    entry = celery_app.conf.beat_schedule["cleanup-outbox"]

    assert entry["task"] == "app.celery_jobs.cleanup_outbox"
    assert entry["schedule"] == 3600.0


def test_every_scheduled_job_exists():
    # a typo in "task" would make Beat send jobs that no worker knows
    for name, entry in celery_app.conf.beat_schedule.items():
        assert entry["task"] in celery_app.tasks, name


# ---------- Task 5: relay_outbox ----------


def reload(session, row) -> OutboxEvent:
    session.expire_all()
    return session.get(OutboxEvent, row.id)


def test_relay_outbox_sends_a_due_row_and_returns_the_count(session, fake_redis):
    row = add_row(session, "a", OutboxStatus.PENDING)

    assert relay_outbox() == 1

    row = reload(session, row)
    assert row.status == OutboxStatus.SENT
    assert row.sent_at is not None
    stream, fields = fake_redis.calls[0]
    assert stream == settings.EVENT_STREAM
    assert fields["event_type"] == EventType.TASK_CREATED
    assert len(fake_redis.calls) == 1


def test_relay_outbox_sends_several_rows_in_one_run(session, fake_redis):
    add_row(session, "a", OutboxStatus.PENDING)
    add_row(session, "b", OutboxStatus.PENDING)

    assert relay_outbox() == 2
    assert len(fake_redis.calls) == 2


def test_relay_outbox_with_nothing_to_send_returns_zero(fake_redis):
    assert relay_outbox() == 0
    assert fake_redis.calls == []


def test_relay_outbox_does_not_send_a_row_twice(session, fake_redis):
    add_row(session, "a", OutboxStatus.PENDING)

    assert relay_outbox() == 1
    assert relay_outbox() == 0
    assert len(fake_redis.calls) == 1


def test_relay_outbox_skips_rows_that_are_not_due_yet(session, fake_redis):
    row = add_row(session, "a", OutboxStatus.PENDING)
    row.next_attempt_at = current_time() + timedelta(minutes=5)
    session.add(row)
    session.commit()

    assert relay_outbox() == 0
    assert fake_redis.calls == []


def test_relay_outbox_runs_through_celery_machinery(session, fake_redis):
    add_row(session, "a", OutboxStatus.PENDING)

    result = relay_outbox.apply()

    assert result.successful()
    assert result.result == 1


def test_relay_outbox_redis_down_is_not_an_error(session, fake_redis):
    row = add_row(session, "a", OutboxStatus.PENDING)
    fake_redis.error = RedisConnectionError("down")

    assert relay_outbox() == 0

    row = reload(session, row)
    assert row.status == OutboxStatus.PENDING
    assert row.attempts == 1


def test_relay_outbox_logs_only_when_it_sent_something(session, fake_redis, caplog):
    with caplog.at_level(logging.INFO, logger="app.celery_jobs"):
        relay_outbox()
    assert "Outbox relay sent" not in caplog.text

    add_row(session, "a", OutboxStatus.PENDING)
    with caplog.at_level(logging.INFO, logger="app.celery_jobs"):
        relay_outbox()
    assert "Outbox relay sent 1 events" in caplog.text


def test_relay_outbox_logs_and_reraises_errors(monkeypatch, caplog):
    def broken():
        raise RuntimeError("db is down")

    monkeypatch.setattr("app.celery_jobs.relay_once", broken)

    with (
        caplog.at_level(logging.ERROR, logger="app.celery_jobs"),
        pytest.raises(RuntimeError, match="db is down"),
    ):
        relay_outbox()

    assert "Outbox relay failed" in caplog.text


def test_failed_relay_shows_up_as_a_failed_job(monkeypatch):
    def broken():
        raise RuntimeError("db is down")

    monkeypatch.setattr("app.celery_jobs.relay_once", broken)

    result = relay_outbox.apply()

    assert result.failed()
    assert isinstance(result.result, RuntimeError)


def test_relay_is_scheduled_every_poll_interval_and_expires():
    entry = celery_app.conf.beat_schedule["relay-outbox"]

    assert entry["task"] == "app.celery_jobs.relay_outbox"
    assert entry["schedule"] == settings.OUTBOX_POLL_SECONDS
    assert entry["options"]["expires"] == 10


def test_relay_expiry_is_longer_than_its_interval():
    # shorter than the interval and every message would expire before a worker takes it
    entry = celery_app.conf.beat_schedule["relay-outbox"]

    assert entry["options"]["expires"] > entry["schedule"]
