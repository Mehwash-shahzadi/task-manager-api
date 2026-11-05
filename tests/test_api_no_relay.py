"""Task 6: the API no longer relays outbox events. Only the Celery job does."""

import ast
import asyncio
import json
import threading
from pathlib import Path

import pytest
from fastapi import Response
from sqlmodel import select

from app.celery_jobs import relay_outbox
from app.enums import EventType
from app.models.outbox import OutboxEvent, OutboxStatus
from app.routers.tasks import create_task
from app.schemas.task import TaskCreate
from tests.fakes import FakeCache

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def imported_modules(path: Path) -> set[str]:
    """Names imported by a file. Reads the code itself, so comments do not count."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def defined_names(path: Path) -> set[str]:
    """Functions, classes and variables defined at the top of a file (comments do not count)."""
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


# ---------- the thread code is gone ----------


def test_outbox_has_no_relay_thread_function():
    assert "run_relay" not in defined_names(APP_DIR / "messaging" / "outbox.py")


def test_outbox_has_no_cleanup_timer_constant():
    assert "CLEANUP_INTERVAL_SECONDS" not in defined_names(
        APP_DIR / "messaging" / "outbox.py"
    )


def test_outbox_keeps_the_functions_celery_uses():
    names = defined_names(APP_DIR / "messaging" / "outbox.py")

    assert {"backoff_seconds", "relay_once", "cleanup_sent"} <= names


def test_main_has_no_lifespan_function():
    assert "lifespan" not in defined_names(APP_DIR / "main.py")


@pytest.mark.parametrize("filename", ["main.py", "messaging/outbox.py"])
def test_no_threading_import_for_the_relay(filename):
    assert "threading" not in imported_modules(APP_DIR / filename)


def test_outbox_no_longer_needs_the_time_import():
    # the only user of `time` was the hourly cleanup timer inside run_relay
    assert "time" not in imported_modules(APP_DIR / "messaging" / "outbox.py")


def test_main_does_not_import_run_relay():
    assert "app.messaging.outbox" not in imported_modules(APP_DIR / "main.py")


# ---------- starting the API starts nothing in the background ----------


def test_starting_and_stopping_the_api_starts_no_thread():
    from app.main import app

    before = {t.ident for t in threading.enumerate()}

    async def run():
        async with app.router.lifespan_context(app):
            return {t.ident for t in threading.enumerate()}

    during = asyncio.run(run())

    assert during == before


# ---------- creating a task only writes to the outbox ----------


@pytest.fixture
def no_cache(monkeypatch):
    monkeypatch.setattr("app.cache.cache_invalidate", lambda *keys: None)


def test_create_task_leaves_the_event_pending_and_sends_nothing(
    session, fake_redis, no_cache
):
    create_task(TaskCreate(title="no thread"), session, Response(), FakeCache())

    row = session.exec(select(OutboxEvent)).one()
    assert row.status == OutboxStatus.PENDING
    assert row.event_type == EventType.TASK_CREATED
    assert row.payload["data"]["title"] == "no thread"
    # nothing relayed it: the API does not touch the stream any more
    assert fake_redis.calls == []


def test_the_celery_job_then_sends_the_event_the_api_left_behind(
    session, fake_redis, no_cache
):
    create_task(TaskCreate(title="via celery"), session, Response(), FakeCache())
    session.expire_all()
    event_id = session.exec(select(OutboxEvent)).one().payload["event_id"]

    assert relay_outbox() == 1

    session.expire_all()
    row = session.exec(select(OutboxEvent)).one()
    assert row.status == OutboxStatus.SENT
    _stream, fields = fake_redis.calls[0]
    sent = json.loads(fields["payload"])
    assert sent["event_id"] == event_id
    assert sent["event_type"] == EventType.TASK_CREATED
    assert sent["data"]["title"] == "via celery"


def test_a_task_created_while_beat_is_stopped_waits_then_is_sent(
    session, fake_redis, no_cache
):
    # Beat stopped = relay_outbox never runs. The row must wait, not be lost.
    create_task(TaskCreate(title="waits"), session, Response(), FakeCache())
    session.expire_all()
    assert session.exec(select(OutboxEvent)).one().status == OutboxStatus.PENDING
    assert fake_redis.calls == []

    # Beat comes back
    assert relay_outbox() == 1
    assert len(fake_redis.calls) == 1
