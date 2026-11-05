"""What the database itself enforces: stored values, constraints, defaults, indexes, locks."""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

from app.enums import TaskStatus
from app.models.outbox import OutboxEvent
from app.models.task import DESCRIPTION_MAX_LENGTH, TITLE_MAX_LENGTH, Task

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------- M4: text lengths ----------


def test_task_text_columns_have_a_length_limit():
    assert Task.__table__.c.title.type.length == TITLE_MAX_LENGTH
    assert Task.__table__.c.description.type.length == DESCRIPTION_MAX_LENGTH


def test_outbox_payload_is_jsonb():
    assert isinstance(OutboxEvent.__table__.c.payload.type, JSONB)


def test_relay_index_exists_on_status_and_next_attempt():
    indexes = {
        i.name: [c.name for c in i.columns] for i in OutboxEvent.__table__.indexes
    }

    assert indexes["ix_outbox_event_status_next_attempt"] == [
        "status",
        "next_attempt_at",
    ]


def test_constraints_and_indexes_have_a_naming_convention():
    convention = SQLModel.metadata.naming_convention

    assert convention["pk"] == "pk_%(table_name)s"
    assert convention["ix"] == "ix_%(column_0_label)s"


# ---------- M1: the database stores the values ----------


def test_status_is_stored_as_lowercase_text(session):
    session.add(Task(title="a", status=TaskStatus.IN_PROGRESS))
    session.commit()

    stored = session.execute(text("select status::text from task")).scalar_one()

    assert stored == "in_progress"


def test_status_comes_back_as_the_enum(session):
    session.add(Task(title="a", status=TaskStatus.DONE))
    session.commit()
    session.expire_all()

    assert session.query(Task).one().status == TaskStatus.DONE


# ---------- M3: the database refuses an empty title ----------


def test_database_refuses_an_empty_title(session):
    session.add(Task(title=""))

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_the_rule_has_the_convention_name():
    names = {c.name for c in Task.__table__.constraints}

    assert "ck_task_title_not_empty" in names


# ---------- L27: migrations take a lock ----------


def test_env_py_takes_advisory_lock_before_migrating():
    source = (PROJECT_ROOT / "alembic/env.py").read_text()

    lock = source.index("pg_advisory_xact_lock")
    assert lock < source.index(
        "context.run_migrations()", source.index("def run_migrations_online")
    )


# ---------- M2: the database fills in defaults ----------


def test_raw_insert_without_defaulted_columns(session):
    session.execute(
        text("INSERT INTO task (id, title) VALUES (gen_random_uuid(), 'raw')")
    )
    session.execute(
        text(
            "INSERT INTO outbox_event (id, event_type, payload) VALUES (gen_random_uuid(), 'e', '{}')"
        )
    )
    session.commit()

    task = session.execute(
        text("SELECT status, created_at, updated_at FROM task")
    ).one()
    event = session.execute(
        text("SELECT status, attempts, bad_attempts, next_attempt_at FROM outbox_event")
    ).one()
    assert task.status == "pending"
    assert task.created_at and task.updated_at
    assert (event.status, event.attempts, event.bad_attempts) == ("pending", 0, 0)
    assert event.next_attempt_at


def test_migration_defaults_match_models():
    source = (
        PROJECT_ROOT / "alembic/versions/d3a7c1e90b42_add_server_defaults.py"
    ).read_text()

    for table in ("task", "outbox_event"):
        for column in Task.__table__.c if table == "task" else OutboxEvent.__table__.c:
            if column.server_default is not None:
                assert f'("{table}", "{column.name}"' in source
