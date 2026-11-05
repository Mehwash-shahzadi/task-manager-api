import logging
from datetime import timedelta

import pytest
from redis import ConnectionError as RedisConnectionError
from redis import DataError
from redis import TimeoutError as RedisTimeoutError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.config import settings
from app.enums import EventType
from app.messaging import monitor, outbox
from app.messaging.outbox import backoff_seconds, cleanup_sent, relay_once
from app.messaging.publisher import add_to_outbox
from app.models.base import current_time
from app.models.outbox import OutboxEvent, OutboxStatus


def add_row(session, name="a") -> OutboxEvent:
    row = OutboxEvent(event_type=EventType.TASK_CREATED, payload={"event_id": name})
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def make_due(session) -> None:
    session.execute(
        text("update outbox_event set next_attempt_at = now() - interval '1 second'")
    )
    session.commit()


def reload(session, row) -> OutboxEvent:
    session.expire_all()
    return session.get(OutboxEvent, row.id)


# backoff


def test_backoff_values():
    assert [backoff_seconds(n) for n in range(1, 9)] == [2, 4, 8, 16, 32, 60, 60, 60]


def test_backoff_does_not_overflow():
    assert backoff_seconds(100_000) == 60


# relay: success


def test_relay_sends_and_marks_sent(session, fake_redis):
    row = add_row(session)

    assert relay_once() == 1

    row = reload(session, row)
    assert row.status == OutboxStatus.SENT
    assert row.sent_at is not None
    stream, fields = fake_redis.calls[0]
    assert stream == settings.EVENT_STREAM
    assert fields["event_type"] == EventType.TASK_CREATED
    assert '"event_id": "a"' in fields["payload"]


def test_relay_ignores_rows_that_are_not_due_yet(session, fake_redis):
    row = add_row(session)
    row.next_attempt_at = current_time() + timedelta(minutes=5)
    session.add(row)
    session.commit()

    assert relay_once() == 0
    assert fake_redis.calls == []


# relay: Redis is down


@pytest.mark.parametrize(
    "error", [RedisConnectionError("down"), RedisTimeoutError("slow")]
)
def test_outage_never_marks_dead(session, fake_redis, error):
    row = add_row(session)
    fake_redis.error = error

    for _ in range(100):
        relay_once()
        make_due(session)

    row = reload(session, row)
    assert row.status == OutboxStatus.PENDING
    assert row.attempts == 100
    assert row.bad_attempts == 0
    assert row.last_error


def test_outage_schedules_a_later_retry(session, fake_redis):
    row = add_row(session)
    fake_redis.error = RedisConnectionError("down")

    relay_once()

    row = reload(session, row)
    assert row.attempts == 1
    assert row.next_attempt_at > current_time()


def test_outage_stops_the_batch(session, fake_redis):
    add_row(session, "a")
    add_row(session, "b")
    fake_redis.error = RedisConnectionError("down")

    assert relay_once() == 0
    assert len(fake_redis.calls) == 1


def test_event_is_sent_after_redis_comes_back(session, fake_redis):
    row = add_row(session)
    fake_redis.error = RedisConnectionError("down")
    relay_once()

    fake_redis.error = None
    make_due(session)

    assert relay_once() == 1
    assert reload(session, row).status == OutboxStatus.SENT


# relay: the event itself is broken


def test_bad_event_becomes_dead_after_max_bad_attempts(session, fake_redis):
    row = add_row(session)
    fake_redis.error = DataError("bad event")

    for _ in range(settings.OUTBOX_MAX_BAD_ATTEMPTS):
        relay_once()
        make_due(session)

    row = reload(session, row)
    assert row.status == OutboxStatus.DEAD
    assert row.bad_attempts == settings.OUTBOX_MAX_BAD_ATTEMPTS


def test_bad_event_does_not_block_the_next_one(session, fake_redis):
    bad = add_row(session, "bad")
    good = add_row(session, "good")

    original = fake_redis.xadd

    def xadd(stream, fields, **kwargs):
        if '"bad"' in fields["payload"]:
            raise DataError("bad event")
        return original(stream, fields, **kwargs)

    fake_redis.xadd = xadd

    assert relay_once() == 1
    assert reload(session, bad).bad_attempts == 1
    assert reload(session, good).status == OutboxStatus.SENT


# add_to_outbox


def test_add_to_outbox_saves_with_the_callers_commit(session):
    add_to_outbox(session, EventType.TASK_CREATED, {"id": "1"})
    session.commit()

    row = session.exec(select(OutboxEvent)).one()
    assert row.status == OutboxStatus.PENDING
    assert row.payload["event_type"] == EventType.TASK_CREATED
    assert row.payload["data"] == {"id": "1"}
    assert row.payload["event_id"]


def test_add_to_outbox_does_not_commit_by_itself(session, test_engine):
    add_to_outbox(session, EventType.TASK_CREATED, {"id": "1"})
    session.rollback()

    assert session.exec(select(OutboxEvent)).all() == []


def test_add_to_outbox_rejects_data_that_is_not_json(session):
    with pytest.raises(TypeError):
        add_to_outbox(session, EventType.TASK_CREATED, {"id": object()})

    assert list(session.new) == []


# cleanup


def test_cleanup_deletes_only_old_sent_rows(session, fake_redis):
    old = current_time() - timedelta(days=settings.OUTBOX_RETENTION_DAYS + 1)
    rows = {}
    for name, status, sent_at in [
        ("old_sent", OutboxStatus.SENT, old),
        ("new_sent", OutboxStatus.SENT, current_time()),
        ("old_pending", OutboxStatus.PENDING, None),
        ("old_dead", OutboxStatus.DEAD, None),
    ]:
        row = add_row(session, name)
        row.status, row.sent_at, row.created_at = status, sent_at, old
        session.add(row)
        rows[name] = row
    session.commit()

    assert cleanup_sent() == 1

    session.expire_all()
    left = {r.payload["event_id"] for r in session.exec(select(OutboxEvent)).all()}
    assert left == {"new_sent", "old_pending", "old_dead"}


def add_outbox_row(session):
    from app.enums import EventType
    from app.messaging.publisher import add_to_outbox

    add_to_outbox(session, EventType.TASK_CREATED, {"id": "1"})
    session.commit()


# ---------- C5: status enum ----------


def test_outbox_status_values_are_the_same_text_as_before():
    assert OutboxStatus.PENDING == "pending"
    assert [s.value for s in OutboxStatus] == ["pending", "sent", "dead"]


def test_outbox_status_column_stays_a_plain_string():
    assert OutboxEvent.__table__.c.status.type.__class__.__name__ in (
        "String",
        "AutoString",
    )


# ---------- M13: cleanup in batches ----------


def test_cleanup_deletes_more_rows_than_one_batch(session, fake_redis, monkeypatch):
    monkeypatch.setattr(outbox, "CLEANUP_BATCH_SIZE", 2)
    old = current_time() - timedelta(days=30)
    for i in range(5):
        session.add(
            OutboxEvent(
                event_type=EventType.TASK_CREATED,
                payload={"event_id": str(i)},
                status=OutboxStatus.SENT,
                sent_at=old,
            )
        )
    session.commit()

    assert outbox.cleanup_sent() == 5
    session.expire_all()
    assert session.exec(select(OutboxEvent)).all() == []


def add_row_with_status(session, status):
    session.add(
        OutboxEvent(event_type=EventType.TASK_CREATED, payload={}, status=status)
    )
    session.commit()


def test_pending_count_counts_only_pending_rows(session, test_engine, monkeypatch):
    monkeypatch.setattr("app.messaging.monitor.get_engine", lambda: test_engine)
    add_row_with_status(session, OutboxStatus.PENDING)
    add_row_with_status(session, OutboxStatus.PENDING)
    add_row_with_status(session, OutboxStatus.SENT)
    session.commit()

    assert monitor.pending_count() == 2


def test_database_outage_logs_one_warning_without_a_traceback(monkeypatch, caplog):
    from app.celery_jobs import relay_outbox

    def down():
        raise OperationalError("select 1", {}, Exception("db down"))

    monkeypatch.setattr("app.celery_jobs.relay_once", down)

    with caplog.at_level(logging.WARNING, logger="app.celery_jobs"):
        result = relay_outbox.apply()

    assert result.failed()
    assert "database unavailable" in caplog.text
    assert all(
        r.exc_info is None for r in caplog.records if r.name == "app.celery_jobs"
    )


# ---------- L22: no database lock while the relay talks to Redis ----------


def test_no_row_lock_is_held_while_sending_to_redis(session, fake_redis, test_engine):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.messaging.outbox import relay_once

    add_outbox_row(session)
    row_id = session.exec(select(OutboxEvent)).one().id
    lock_results = []

    def xadd_that_tries_to_lock(stream, fields, **kwargs):
        # Another connection asks for the same row, and refuses to wait.
        with Session(test_engine) as other:
            try:
                other.execute(
                    text(
                        "SELECT id FROM outbox_event WHERE id = :id FOR UPDATE NOWAIT"
                    ),
                    {"id": row_id},
                )
                lock_results.append("free")
            except DBAPIError:  # The database says: the row is locked.
                lock_results.append("locked")

    fake_redis.xadd = xadd_that_tries_to_lock

    assert relay_once() == 1
    assert lock_results == ["free"]


def test_a_claimed_row_is_hidden_from_a_second_relay_run(session, fake_redis):
    from app.messaging.outbox import relay_once

    add_outbox_row(session)
    second_run = []

    def xadd_that_starts_another_run(stream, fields, **kwargs):
        second_run.append(relay_once())  # A second relay run, while the first sends.

    fake_redis.xadd = xadd_that_starts_another_run

    assert relay_once() == 1
    assert second_run == [0]  # The row was leased, so the second run found nothing.


def test_when_redis_fails_the_untried_rows_are_released_at_once(session, fake_redis):
    from redis.exceptions import ConnectionError as RedisConnectionError
    from sqlalchemy import func

    from app.messaging.outbox import relay_once

    add_outbox_row(session)
    add_outbox_row(session)
    fake_redis.error = RedisConnectionError("down")

    relay_once()

    session.expire_all()
    rows = session.exec(select(OutboxEvent).order_by(OutboxEvent.created_at)).all()
    database_now = session.exec(select(func.now())).one()
    tried, untried = rows
    assert tried.attempts == 1
    assert tried.next_attempt_at > database_now  # Backoff.
    assert untried.attempts == 0
    assert untried.next_attempt_at <= database_now  # Due again right away.


def test_a_crash_between_claim_and_record_loses_nothing(session, fake_redis):
    from sqlalchemy import text

    from app.messaging.outbox import CLAIM_LEASE_SECONDS, relay_once

    add_outbox_row(session)

    def xadd_that_crashes(stream, fields, **kwargs):
        raise RuntimeError("worker killed")

    fake_redis.xadd = xadd_that_crashes
    with pytest.raises(RuntimeError):
        relay_once()

    session.expire_all()
    row = session.exec(select(OutboxEvent)).one()
    assert row.status == "pending"  # Still waiting, not lost.
    # The lease hides it for a while, then the next run sends it.
    lease = (row.next_attempt_at - row.updated_at).total_seconds()
    assert 0 < lease <= CLAIM_LEASE_SECONDS + 1

    del fake_redis.xadd  # Back to the normal fake.
    session.execute(
        text("UPDATE outbox_event SET next_attempt_at = now() - interval '1 second'")
    )
    session.commit()
    assert relay_once() == 1


# ---------- F1: a failed attempt is recorded in one place ----------


def test_record_failure_sets_the_count_error_and_retry_time(session):
    from datetime import UTC, datetime, timedelta

    from app.messaging.outbox import MAX_ERROR_LENGTH, backoff_seconds, record_failure

    row = OutboxEvent(event_type="task.created", payload={})
    now = datetime(2026, 1, 1, tzinfo=UTC)

    record_failure(row, ValueError("x" * 1000), now)

    assert row.attempts == 1
    assert row.bad_attempts == 0  # Only the caller counts a broken event.
    assert len(row.last_error) == MAX_ERROR_LENGTH
    assert row.next_attempt_at == now + timedelta(seconds=backoff_seconds(1))
