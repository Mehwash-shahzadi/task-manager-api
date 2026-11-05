"""Task 11: the mark_overdue_tasks Celery job and its Beat schedule."""

import logging

import pytest

from app.celery_app import celery_app


@pytest.fixture(autouse=True)
def clean_tables():
    """These tests do not touch the database, so skip the conftest cleanup."""
    yield


def test_job_is_registered_under_its_full_name():
    import app.celery_jobs  # noqa: F401  Celery loads the jobs module lazily

    assert "app.celery_jobs.mark_overdue_tasks" in celery_app.tasks


def test_job_returns_the_count(monkeypatch):
    from app.celery_jobs import mark_overdue_tasks

    monkeypatch.setattr("app.celery_jobs.mark_overdue", lambda: 3)

    assert mark_overdue_tasks() == 3


def test_job_runs_through_celery_machinery(monkeypatch):
    from app.celery_jobs import mark_overdue_tasks

    monkeypatch.setattr("app.celery_jobs.mark_overdue", lambda: 2)

    result = mark_overdue_tasks.apply()

    assert result.successful()
    assert result.result == 2


def test_job_logs_a_summary_only_when_something_was_flagged(monkeypatch, caplog):
    from app.celery_jobs import mark_overdue_tasks

    monkeypatch.setattr("app.celery_jobs.mark_overdue", lambda: 0)
    with caplog.at_level(logging.INFO, logger="app.celery_jobs"):
        mark_overdue_tasks()
    assert "Marked" not in caplog.text

    monkeypatch.setattr("app.celery_jobs.mark_overdue", lambda: 2)
    with caplog.at_level(logging.INFO, logger="app.celery_jobs"):
        mark_overdue_tasks()
    assert "Marked 2 tasks overdue" in caplog.text


def test_job_logs_and_reraises_errors(monkeypatch, caplog):
    from app.celery_jobs import mark_overdue_tasks

    def broken():
        raise RuntimeError("db is down")

    monkeypatch.setattr("app.celery_jobs.mark_overdue", broken)

    with (
        caplog.at_level(logging.ERROR, logger="app.celery_jobs"),
        pytest.raises(RuntimeError, match="db is down"),
    ):
        mark_overdue_tasks()

    assert "Overdue check failed" in caplog.text


def test_failed_check_shows_up_as_a_failed_job(monkeypatch):
    from app.celery_jobs import mark_overdue_tasks

    def broken():
        raise RuntimeError("db is down")

    monkeypatch.setattr("app.celery_jobs.mark_overdue", broken)

    assert mark_overdue_tasks.apply().failed()


def test_job_is_scheduled_every_minute_and_expires_before_the_next_run():
    entry = celery_app.conf.beat_schedule["mark-overdue-tasks"]

    assert entry["task"] == "app.celery_jobs.mark_overdue_tasks"
    assert entry["schedule"] == 60.0
    assert entry["options"]["expires"] == 50


def test_every_scheduled_job_still_exists():
    for name, entry in celery_app.conf.beat_schedule.items():
        assert entry["task"] in celery_app.tasks, name
