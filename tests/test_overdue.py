"""Task 11: mark_overdue() flags each overdue, not-done task once."""

import logging
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.cache import LIST_KEY, task_key
from app.enums import TaskStatus
from app.models.base import current_time
from app.models.task import Task
from app.overdue import mark_overdue

HOUR = timedelta(hours=1)


@pytest.fixture
def invalidated(monkeypatch, test_engine):
    """mark_overdue uses the test database. Returns the list of cache keys it cleared."""
    monkeypatch.setattr("app.overdue.get_engine", lambda: test_engine)
    calls = []
    monkeypatch.setattr("app.cache.cache_invalidate", lambda *keys: calls.append(keys))
    return calls


def add_task(
    session, title="t", due=None, status=TaskStatus.PENDING, marker=None
) -> Task:
    task = Task(title=title, due_date=due, status=status, overdue_notified_at=marker)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def fresh(test_engine, task) -> Task:
    """Reads the row with a brand new session, so only committed data is visible."""
    with Session(test_engine) as other:
        return other.get(Task, task.id)


# ---------- which rows are flagged ----------


def test_overdue_rows_are_flagged(session, test_engine, invalidated):
    pending = add_task(session, "pending", current_time() - HOUR)
    working = add_task(
        session, "working", current_time() - HOUR, TaskStatus.IN_PROGRESS
    )

    assert mark_overdue() == 2

    assert fresh(test_engine, pending).overdue_notified_at is not None
    assert fresh(test_engine, working).overdue_notified_at is not None


def test_rows_that_must_not_be_flagged_are_left_alone(
    session, test_engine, invalidated
):
    old_marker = current_time() - 2 * HOUR
    done = add_task(session, "done", current_time() - HOUR, TaskStatus.DONE)
    future = add_task(session, "future", current_time() + HOUR)
    no_date = add_task(session, "no date")
    already = add_task(session, "already", current_time() - HOUR, marker=old_marker)

    assert mark_overdue() == 0

    assert fresh(test_engine, done).overdue_notified_at is None
    assert fresh(test_engine, future).overdue_notified_at is None
    assert fresh(test_engine, no_date).overdue_notified_at is None
    assert fresh(test_engine, already).overdue_notified_at == old_marker


def test_only_the_overdue_rows_in_a_mixed_table_are_flagged(
    session, test_engine, invalidated
):
    late = add_task(session, "late", current_time() - HOUR)
    add_task(session, "future", current_time() + HOUR)
    add_task(session, "done", current_time() - HOUR, TaskStatus.DONE)

    assert mark_overdue() == 1
    assert fresh(test_engine, late).overdue_notified_at is not None


def test_empty_table(invalidated):
    assert mark_overdue() == 0


def test_marker_is_the_time_of_the_run(session, test_engine, invalidated):
    task = add_task(session, "late", current_time() - HOUR)
    before = current_time()

    mark_overdue()

    marker = fresh(test_engine, task).overdue_notified_at
    assert before <= marker <= current_time()


def test_all_rows_in_one_run_get_the_same_marker(session, test_engine, invalidated):
    a = add_task(session, "a", current_time() - HOUR)
    b = add_task(session, "b", current_time() - HOUR)

    mark_overdue()

    assert (
        fresh(test_engine, a).overdue_notified_at
        == fresh(test_engine, b).overdue_notified_at
    )


# ---------- running twice is safe ----------


def test_second_run_flags_nothing_and_changes_nothing(
    session, test_engine, invalidated
):
    task = add_task(session, "late", current_time() - HOUR)

    assert mark_overdue() == 1
    first = fresh(test_engine, task).overdue_notified_at

    assert mark_overdue() == 0
    assert fresh(test_engine, task).overdue_notified_at == first


def test_row_is_flagged_again_after_a_new_due_date(session, test_engine, invalidated):
    task = add_task(session, "late", current_time() - HOUR)
    assert mark_overdue() == 1

    # what update_task does when the due date changes
    session.expire_all()
    row = session.get(Task, task.id)
    row.due_date = current_time() - timedelta(minutes=5)
    row.overdue_notified_at = None
    session.add(row)
    session.commit()

    assert mark_overdue() == 1


# ---------- batch limit and locking ----------


def test_batch_limit(session, invalidated, monkeypatch):
    monkeypatch.setattr("app.overdue.BATCH_SIZE", 2)
    for i in range(3):
        add_task(session, f"late {i}", current_time() - HOUR)

    assert mark_overdue() == 2
    assert mark_overdue() == 1
    assert mark_overdue() == 0


def test_oldest_deadline_is_flagged_first(
    session, test_engine, invalidated, monkeypatch
):
    monkeypatch.setattr("app.overdue.BATCH_SIZE", 1)
    older = add_task(session, "older", current_time() - 3 * HOUR)
    newer = add_task(session, "newer", current_time() - HOUR)

    mark_overdue()

    assert fresh(test_engine, older).overdue_notified_at is not None
    assert fresh(test_engine, newer).overdue_notified_at is None


def test_a_row_locked_by_another_run_is_skipped_not_waited_for(
    session, test_engine, invalidated
):
    locked = add_task(session, "locked", current_time() - HOUR)
    free = add_task(session, "free", current_time() - HOUR)

    with Session(test_engine) as other:
        other.exec(select(Task).where(Task.id == locked.id).with_for_update()).one()

        assert mark_overdue() == 1  # does not hang on the locked row

    assert fresh(test_engine, free).overdue_notified_at is not None
    assert fresh(test_engine, locked).overdue_notified_at is None


# ---------- cache ----------


def test_cache_keys_of_flagged_rows_are_cleared(session, invalidated):
    a = add_task(session, "a", current_time() - HOUR)
    b = add_task(session, "b", current_time() - HOUR)
    other = add_task(session, "not late", current_time() + HOUR)

    mark_overdue()

    assert len(invalidated) == 1
    keys = set(invalidated[0])
    assert keys == {LIST_KEY, task_key(a.id), task_key(b.id)}
    assert task_key(other.id) not in keys


def test_no_cache_call_when_nothing_was_flagged(session, invalidated):
    add_task(session, "future", current_time() + HOUR)

    mark_overdue()

    assert invalidated == []


def test_cache_is_cleared_after_the_commit(session, test_engine, monkeypatch):
    task = add_task(session, "late", current_time() - HOUR)
    monkeypatch.setattr("app.overdue.get_engine", lambda: test_engine)
    seen = []

    def check(*keys):
        # a brand new session only sees committed data
        seen.append(fresh(test_engine, task).overdue_notified_at)

    monkeypatch.setattr("app.cache.cache_invalidate", check)

    mark_overdue()

    assert len(seen) == 1
    assert seen[0] is not None


# ---------- logging ----------


def test_one_log_line_per_flagged_task(session, invalidated, caplog):
    a = add_task(session, "first late", current_time() - HOUR)
    add_task(session, "second late", current_time() - HOUR)
    add_task(session, "not late", current_time() + HOUR)

    with caplog.at_level(logging.INFO, logger="app.overdue"):
        mark_overdue()

    lines = [r.getMessage() for r in caplog.records if "Task overdue" in r.getMessage()]
    assert len(lines) == 2
    assert any(str(a.id) in line and "first late" in line for line in lines)
    assert not any("not late" in line for line in lines)
