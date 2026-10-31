from datetime import timedelta

import pytest
from redis import ConnectionError as RedisConnectionError
from redis import DataError
from redis import TimeoutError as RedisTimeoutError
from sqlalchemy import text
from sqlmodel import select

from app.config import settings
from app.events import add_to_outbox
from app.models.base import current_time
from app.models.outbox import DEAD, PENDING, SENT, OutboxEvent
from app.outbox import backoff_seconds, cleanup_sent, relay_once


def add_row(session, name="a") -> OutboxEvent:
    row = OutboxEvent(event_type="task.created", payload={"event_id": name})
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
    assert row.status == SENT
    assert row.sent_at is not None
    stream, fields = fake_redis.calls[0]
    assert stream == settings.EVENT_STREAM
    assert fields["event_type"] == "task.created"
    assert '"event_id": "a"' in fields["payload"]


def test_relay_ignores_rows_that_are_not_due_yet(session, fake_redis):
    row = add_row(session)
    row.next_attempt_at = current_time() + timedelta(minutes=5)
    session.add(row)
    session.commit()

    assert relay_once() == 0
    assert fake_redis.calls == []


# relay: Redis is down

@pytest.mark.parametrize("error", [RedisConnectionError("down"), RedisTimeoutError("slow")])
def test_outage_never_marks_dead(session, fake_redis, error):
    row = add_row(session)
    fake_redis.error = error

    for _ in range(100):
        relay_once()
        make_due(session)

    row = reload(session, row)
    assert row.status == PENDING
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
    assert reload(session, row).status == SENT


# relay: the event itself is broken

def test_bad_event_becomes_dead_after_max_bad_attempts(session, fake_redis):
    row = add_row(session)
    fake_redis.error = DataError("bad event")

    for _ in range(settings.OUTBOX_MAX_BAD_ATTEMPTS):
        relay_once()
        make_due(session)

    row = reload(session, row)
    assert row.status == DEAD
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
    assert reload(session, good).status == SENT


# add_to_outbox

def test_add_to_outbox_saves_with_the_callers_commit(session):
    add_to_outbox(session, "task.created", {"id": "1"})
    session.commit()

    row = session.exec(select(OutboxEvent)).one()
    assert row.status == PENDING
    assert row.payload["event_type"] == "task.created"
    assert row.payload["data"] == {"id": "1"}
    assert row.payload["event_id"]


def test_add_to_outbox_does_not_commit_by_itself(session, test_engine):
    add_to_outbox(session, "task.created", {"id": "1"})
    session.rollback()

    assert session.exec(select(OutboxEvent)).all() == []


def test_add_to_outbox_rejects_data_that_is_not_json(session):
    with pytest.raises(TypeError):
        add_to_outbox(session, "task.created", {"id": object()})

    assert list(session.new) == []


# cleanup

def test_cleanup_deletes_only_old_sent_rows(session, fake_redis):
    old = current_time() - timedelta(days=settings.OUTBOX_RETENTION_DAYS + 1)
    rows = {}
    for name, status, sent_at in [
        ("old_sent", SENT, old),
        ("new_sent", SENT, current_time()),
        ("old_pending", PENDING, None),
        ("old_dead", DEAD, None),
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
