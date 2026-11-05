"""PATCH locks the task row, so the overdue job cannot mark it while the date is changing."""

from datetime import timedelta

import pytest
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, text

from app.models.base import current_time
from app.models.task import Task
from app.overdue import mark_overdue
from app.routers.tasks import get_task_or_404


@pytest.fixture
def overdue_task(session):
    task = Task(title="late", due_date=current_time() - timedelta(hours=1))
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def test_a_locked_task_is_skipped_by_the_overdue_job(
    session, test_engine, overdue_task, monkeypatch
):
    monkeypatch.setattr("app.overdue.get_engine", lambda: test_engine)
    get_task_or_404(session, overdue_task.id, lock=True)  # What PATCH does.

    assert mark_overdue() == 0  # Skipped: it is locked.

    session.rollback()  # PATCH finished.
    assert mark_overdue() == 1  # Now the job can mark it.


def test_without_the_lock_the_overdue_job_would_not_be_skipped(
    session, test_engine, overdue_task, monkeypatch
):
    monkeypatch.setattr("app.overdue.get_engine", lambda: test_engine)
    get_task_or_404(session, overdue_task.id)  # A plain read takes no lock.

    assert mark_overdue() == 1


def test_the_lock_really_blocks_other_writers(session, test_engine, overdue_task):
    get_task_or_404(session, overdue_task.id, lock=True)

    with Session(test_engine) as other, pytest.raises(DBAPIError):
        other.execute(
            text("SELECT id FROM task WHERE id = :id FOR UPDATE NOWAIT"),
            {"id": overdue_task.id},
        )


def test_patch_still_works_and_clears_the_marker(client, session, overdue_task):
    overdue_task.overdue_notified_at = current_time()
    session.add(overdue_task)
    session.commit()
    future = (current_time() + timedelta(days=1)).isoformat()

    response = client.patch(f"/v1/tasks/{overdue_task.id}", json={"due_date": future})

    assert response.status_code == 200
    assert response.json()["data"]["overdue_notified_at"] is None


def test_patch_of_an_unknown_task_is_still_404(client):
    from uuid import uuid4

    assert client.patch(f"/v1/tasks/{uuid4()}", json={"title": "x"}).status_code == 404


def test_the_patch_route_locks_the_row_it_changes(client, session, monkeypatch):
    from app.routers import tasks as tasks_router

    asked = []
    original = tasks_router.get_task_or_404

    def spy(session, task_id, lock=False):
        asked.append(lock)
        return original(session, task_id, lock)

    monkeypatch.setattr(tasks_router, "get_task_or_404", spy)
    task_id = client.post("/v1/tasks", json={"title": "a"}).json()["data"]["id"]

    client.patch(f"/v1/tasks/{task_id}", json={"title": "b"})

    assert asked == [True]  # PATCH locks the row (a plain read would not).
