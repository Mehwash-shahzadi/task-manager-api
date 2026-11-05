"""Row times come from the database clock, and the outbox has the indexes it needs."""

from datetime import timedelta

from sqlalchemy import func, text
from sqlmodel import select

from app.models.base import current_time
from app.models.outbox import OutboxEvent
from app.models.task import Task


def database_now(session):
    return session.exec(select(func.now())).one()


def test_a_new_task_gets_its_times_from_the_database(session, monkeypatch):
    # If the app clock were a day behind, the stored times must not follow it.
    monkeypatch.setattr(
        "app.models.base.current_time", lambda: current_time() - timedelta(days=1)
    )
    task = Task(title="a")
    session.add(task)
    session.commit()
    session.refresh(task)

    assert abs(task.created_at - database_now(session)) < timedelta(seconds=5)
    assert task.updated_at == task.created_at  # Same insert, same clock reading.


def test_updated_at_is_never_earlier_than_created_at(session):
    task = Task(title="a")
    session.add(task)
    session.commit()
    task.title = "b"
    session.add(task)
    session.commit()
    session.refresh(task)

    assert task.updated_at >= task.created_at


def test_the_api_answer_shows_the_updated_at_that_was_stored(client, session):
    created = client.post("/v1/tasks", json={"title": "a"}).json()["data"]

    patched = client.patch(f"/v1/tasks/{created['id']}", json={"title": "b"}).json()[
        "data"
    ]

    session.expire_all()
    stored = session.get(Task, patched["id"])
    assert patched["updated_at"] > created["updated_at"]
    assert patched["updated_at"] == stored.updated_at.isoformat().replace("+00:00", "Z")


def test_an_outbox_row_is_due_by_the_database_clock(session, monkeypatch):
    monkeypatch.setattr(
        "app.models.base.current_time", lambda: current_time() + timedelta(days=1)
    )
    row = OutboxEvent(event_type="task.created", payload={})
    session.add(row)
    session.commit()
    session.refresh(row)

    # An app clock one day ahead would have delayed the first send by a day.
    assert row.next_attempt_at <= database_now(session)


def test_the_python_clock_is_no_longer_a_default_for_row_times():
    from app.models.base import BaseTable

    for name in ("created_at", "updated_at"):
        assert BaseTable.model_fields[name].default is None
        assert BaseTable.model_fields[name].default_factory is None
    assert OutboxEvent.model_fields["next_attempt_at"].default_factory is None


def index_definitions(session):
    rows = session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'outbox_event'"
        )
    )
    return dict(rows.all())


def test_the_cleanup_has_a_partial_index_on_sent_rows(session):
    definition = index_definitions(session)["ix_outbox_event_sent_at_sent"]

    assert "(sent_at)" in definition
    assert "status" in definition.split("WHERE")[1]
    assert "sent" in definition.split("WHERE")[1]


def test_the_redundant_status_index_is_gone_and_the_relay_index_stays(session):
    names = set(index_definitions(session))

    assert "ix_outbox_event_status" not in names
    assert "ix_outbox_event_status_next_attempt" in names


def add_outbox_row(session):
    from app.enums import EventType
    from app.messaging.publisher import add_to_outbox

    add_to_outbox(session, EventType.TASK_CREATED, {"id": "1"})
    session.commit()


# ---------- M7: the database sets updated_at ----------


def test_a_raw_sql_update_changes_updated_at(session):
    from sqlalchemy import text

    task = Task(title="a")
    session.add(task)
    session.commit()
    session.refresh(task)
    before = task.updated_at

    session.execute(text("UPDATE task SET title = 'b' WHERE id = :id"), {"id": task.id})
    session.commit()
    session.refresh(task)

    assert task.title == "b"
    assert task.updated_at > before


def test_the_outbox_table_has_the_trigger_too(session):
    from sqlalchemy import text

    names = session.execute(
        text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
    ).scalars()

    assert {"task_set_updated_at", "outbox_event_set_updated_at"} <= set(names)


# ---------- M8: the relay takes the time from the database ----------


def test_the_relay_no_longer_reads_the_python_clock():
    from app.messaging import outbox

    assert not hasattr(outbox, "current_time")


def test_retry_time_is_the_database_time_plus_the_backoff(session, fake_redis):
    from datetime import timedelta

    from redis.exceptions import ConnectionError as RedisConnectionError
    from sqlalchemy import func

    from app.messaging.outbox import backoff_seconds, relay_once

    add_outbox_row(session)
    fake_redis.error = RedisConnectionError("down")

    relay_once()

    session.expire_all()
    row = session.exec(select(OutboxEvent)).one()
    database_now = session.exec(select(func.now())).one()
    expected = timedelta(seconds=backoff_seconds(1))
    # The relay read "now" a moment before this line, so allow a small gap.
    gap = database_now + expected - row.next_attempt_at
    assert timedelta(0) <= gap < timedelta(seconds=5)
