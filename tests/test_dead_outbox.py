"""Dead outbox rows: counted, reported, replayed and cleaned up."""

import logging

from sqlalchemy import text
from sqlmodel import select

from app.config import settings
from app.messaging.monitor import check_dead, dead_count
from app.messaging.outbox import cleanup_dead, relay_once
from app.messaging.replay import replay_dead_outbox
from app.models.outbox import OutboxEvent, OutboxStatus
from tests.fakes import FakeRedis


def add_row(session, status=OutboxStatus.DEAD, name="a", age_days=0):
    row = OutboxEvent(
        event_type="task.created",
        payload={"event_id": name},
        status=status,
        bad_attempts=5 if status == OutboxStatus.DEAD else 0,
        last_error="bad data" if status == OutboxStatus.DEAD else None,
    )
    session.add(row)
    session.commit()
    if age_days:
        # The trigger sets updated_at on every update, so switch it off for this one.
        session.execute(text("ALTER TABLE outbox_event DISABLE TRIGGER USER"))
        session.execute(
            text(
                "UPDATE outbox_event SET updated_at = now() - make_interval(days => :d) "
                "WHERE id = :id"
            ),
            {"d": age_days, "id": row.id},
        )
        session.execute(text("ALTER TABLE outbox_event ENABLE TRIGGER USER"))
        session.commit()
    session.refresh(row)
    return row


def test_dead_rows_are_counted_and_other_rows_are_not(session, test_engine):
    add_row(session, OutboxStatus.DEAD)
    add_row(session, OutboxStatus.DEAD, "b")
    add_row(session, OutboxStatus.PENDING, "c")
    add_row(session, OutboxStatus.SENT, "d")

    assert dead_count(test_engine) == 2


def test_check_dead_logs_an_error_with_the_fix_command(session, test_engine, caplog):
    add_row(session)

    with caplog.at_level(logging.ERROR):
        assert check_dead(test_engine) == 1

    assert "1 outbox events are dead" in caplog.text
    assert "python -m app.messaging.replay outbox" in caplog.text


def test_check_dead_is_quiet_when_there_are_none(session, test_engine, caplog):
    add_row(session, OutboxStatus.PENDING)

    with caplog.at_level(logging.ERROR):
        assert check_dead(test_engine) == 0

    assert caplog.text == ""


def test_replay_sets_dead_rows_back_to_pending_with_fresh_tries(session, test_engine):
    row = add_row(session)

    assert replay_dead_outbox(engine=test_engine) == 1

    session.expire_all()
    row = session.get(OutboxEvent, row.id)
    assert row.status == OutboxStatus.PENDING
    assert row.bad_attempts == 0
    assert row.last_error == "bad data"  # Kept, so you can still see what went wrong.


def test_replay_only_touches_dead_rows_and_respects_the_limit(session, test_engine):
    add_row(session, OutboxStatus.DEAD, "a")
    add_row(session, OutboxStatus.DEAD, "b")
    sent = add_row(session, OutboxStatus.SENT, "c")

    assert replay_dead_outbox(limit=1, engine=test_engine) == 1

    session.expire_all()
    assert dead_count(test_engine) == 1
    assert session.get(OutboxEvent, sent.id).status == OutboxStatus.SENT


def test_a_replayed_row_is_sent_by_the_next_relay_run(session, test_engine):
    row = add_row(session)
    replay_dead_outbox(engine=test_engine)
    redis = FakeRedis()

    assert relay_once(test_engine, redis) == 1

    session.expire_all()
    assert session.get(OutboxEvent, row.id).status == OutboxStatus.SENT


def test_cleanup_deletes_only_old_dead_rows(session, test_engine):
    old = add_row(session, name="old", age_days=settings.OUTBOX_RETENTION_DAYS + 1)
    new = add_row(session, name="new", age_days=1)
    pending_old = add_row(
        session, OutboxStatus.PENDING, "p", age_days=settings.OUTBOX_RETENTION_DAYS + 1
    )

    old_id, new_id, pending_id = old.id, new.id, pending_old.id

    assert cleanup_dead(test_engine) == 1

    ids = {r.id for r in session.exec(select(OutboxEvent)).all()}
    assert old_id not in ids
    assert {new_id, pending_id} <= ids


def test_the_cleanup_job_removes_old_dead_rows_too(session, test_engine, monkeypatch):
    from app import celery_jobs as jobs

    add_row(session, age_days=settings.OUTBOX_RETENTION_DAYS + 1)
    monkeypatch.setattr("app.messaging.outbox.get_engine", lambda: test_engine)

    assert jobs.cleanup_outbox.apply().get() == 1


class Up:
    def ping(self):
        return True


def test_health_shows_the_dead_count(client, test_engine, monkeypatch):
    monkeypatch.setattr("app.main.pending_count", lambda *a: 0)
    monkeypatch.setattr("app.main.stream_backlog", lambda *a: 0)
    monkeypatch.setattr("app.main.dead_count", lambda *a: 3)
    monkeypatch.setattr("app.main.get_engine", lambda: test_engine)
    monkeypatch.setattr("app.main.get_redis_client", Up)
    monkeypatch.setattr("app.main.get_cache_client", Up)

    response = client.get("/health")

    assert response.json()["outbox_dead"] == 3


def test_the_alert_job_is_scheduled_and_registered():
    from app.celery_app import celery_app

    names = {e["task"] for e in celery_app.conf.beat_schedule.values()}
    assert "app.celery_jobs.watch_outbox" in names
    assert "app.celery_jobs.watch_outbox" in celery_app.tasks


def test_the_command_line_rejects_an_unknown_target():
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "app.messaging.replay", "nonsense"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode != 0
    assert "Usage" in done.stderr
