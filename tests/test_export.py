"""Task 7: the export_tasks_csv job."""

import csv
import json
import os
import time
from pathlib import Path

import pytest

from app.celery_app import celery_app
from app.celery_jobs import delete_old_exports, export_tasks_csv
from app.config import Settings, settings
from app.models.task import Task

PROJECT_ROOT = Path(__file__).resolve().parent.parent

HEADER = ["id", "title", "description", "status", "created_at", "updated_at"]


@pytest.fixture
def export_dir(monkeypatch, test_engine, tmp_path):
    """Job reads the test database and writes into a temporary folder."""
    monkeypatch.setattr("app.celery_jobs.get_engine", lambda: test_engine)
    monkeypatch.setattr(settings, "EXPORT_DIR", str(tmp_path))
    return tmp_path


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def add_tasks(session, *tasks):
    for task in tasks:
        session.add(task)
    session.commit()


# ---------- setting ----------


def test_export_dir_is_required_and_listed_in_env_example():
    # config.py has no default: the value must come from the environment or .env
    field = Settings.model_fields["EXPORT_DIR"]
    example = (PROJECT_ROOT / ".env.example").read_text().splitlines()

    assert field.is_required()
    assert "EXPORT_DIR=exports" in example


def test_job_is_registered_under_its_full_name():
    assert "app.celery_jobs.export_tasks_csv" in celery_app.tasks


# ---------- what is written ----------


def test_two_rows_give_a_header_and_two_lines(session, export_dir):
    add_tasks(session, Task(title="a"), Task(title="b"))

    result = export_tasks_csv.apply()

    assert result.successful()
    assert result.result["rows"] == 2
    lines = read_csv(export_dir / result.result["file"])
    assert lines[0] == HEADER
    assert len(lines) == 3


def test_row_content_matches_the_task(session, export_dir):
    task = Task(title="Write docs", description="for the team")
    add_tasks(session, task)

    result = export_tasks_csv.apply()

    _, row = read_csv(export_dir / result.result["file"])
    assert row == [
        str(task.id),
        "Write docs",
        "for the team",
        "pending",
        task.created_at.isoformat(),
        task.updated_at.isoformat(),
    ]


def test_status_is_written_as_its_text_value(session, export_dir):
    from app.enums import TaskStatus

    add_tasks(
        session,
        Task(title="a", status=TaskStatus.IN_PROGRESS),
        Task(title="b", status=TaskStatus.DONE),
    )

    result = export_tasks_csv.apply()

    statuses = {
        row[1]: row[3] for row in read_csv(export_dir / result.result["file"])[1:]
    }
    assert statuses == {"a": "in_progress", "b": "done"}


def test_missing_description_is_an_empty_cell_not_the_word_none(session, export_dir):
    add_tasks(session, Task(title="no description"))

    result = export_tasks_csv.apply()

    _, row = read_csv(export_dir / result.result["file"])
    assert row[2] == ""


def test_commas_quotes_and_new_lines_survive(session, export_dir):
    title = 'He said "hi", then left'
    description = "line one\nline two"
    add_tasks(session, Task(title=title, description=description))

    result = export_tasks_csv.apply()

    _, row = read_csv(export_dir / result.result["file"])
    assert row[1] == title
    assert row[2] == description


def test_non_ascii_text_survives(session, export_dir):
    add_tasks(session, Task(title="چائے لے آؤ ☕ café"))

    result = export_tasks_csv.apply()

    _, row = read_csv(export_dir / result.result["file"])
    assert row[1] == "چائے لے آؤ ☕ café"


# ---------- empty table, folder, file name ----------


def test_zero_rows_gives_a_header_only_file(export_dir):
    result = export_tasks_csv.apply()

    assert result.successful()
    assert result.result["rows"] == 0
    assert read_csv(export_dir / result.result["file"]) == [HEADER]


def test_missing_folder_is_created(monkeypatch, export_dir):
    nested = export_dir / "new" / "folder"
    monkeypatch.setattr(settings, "EXPORT_DIR", str(nested))

    result = export_tasks_csv.apply()

    assert nested.is_dir()
    assert (nested / f"tasks-{result.id}.csv").exists()


def test_file_name_contains_the_job_id_and_lives_in_export_dir(export_dir):
    result = export_tasks_csv.apply()

    assert result.result["file"] == f"tasks-{result.id}.csv"
    assert (export_dir / result.result["file"]).is_file()


def test_each_run_writes_its_own_file(session, export_dir):
    add_tasks(session, Task(title="a"))

    first = export_tasks_csv.apply()
    second = export_tasks_csv.apply()

    assert first.result["file"] != second.result["file"]
    assert len(list(export_dir.glob("tasks-*.csv"))) == 2


# ---------- result and failures ----------


def test_result_is_small_and_json_safe(session, export_dir):
    add_tasks(session, Task(title="a"))

    result = export_tasks_csv.apply()

    assert set(result.result) == {"file", "rows"}
    assert isinstance(result.result["file"], str)
    assert json.loads(json.dumps(result.result)) == result.result


def test_job_does_not_change_the_tasks(session, export_dir):
    add_tasks(session, Task(title="a"), Task(title="b"))

    export_tasks_csv.apply()

    session.expire_all()
    assert {t.title for t in session.query(Task).all()} == {"a", "b"}


def test_database_error_fails_the_job_and_leaves_no_file(monkeypatch, export_dir):
    class BrokenEngine:
        def connect(self, *args, **kwargs):
            raise RuntimeError("db is down")

    monkeypatch.setattr("app.celery_jobs.get_engine", BrokenEngine)

    result = export_tasks_csv.apply()

    assert result.failed()
    assert list(export_dir.glob("*.csv")) == []


# ---------- N5: safe cells, streaming, temporary file ----------


@pytest.mark.parametrize("bad", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
def test_cells_that_look_like_formulas_get_a_leading_quote(session, export_dir, bad):
    add_tasks(session, Task(title=bad, description=bad))

    result = export_tasks_csv.apply()

    _, row = read_csv(export_dir / result.result["file"])
    assert row[1] == "'" + bad
    assert row[2] == "'" + bad


def test_normal_text_is_not_changed(session, export_dir):
    add_tasks(session, Task(title="Buy milk", description="2 liters"))

    result = export_tasks_csv.apply()

    _, row = read_csv(export_dir / result.result["file"])
    assert row[1:3] == ["Buy milk", "2 liters"]


def test_more_rows_than_one_batch_are_all_written(session, export_dir, monkeypatch):
    monkeypatch.setattr("app.celery_jobs.EXPORT_BATCH_SIZE", 2)
    add_tasks(session, *[Task(title=f"t{i}") for i in range(5)])

    result = export_tasks_csv.apply()

    assert result.result["rows"] == 5
    assert len(read_csv(export_dir / result.result["file"])) == 6


def test_no_temporary_file_is_left_after_success(session, export_dir):
    add_tasks(session, Task(title="a"))

    export_tasks_csv.apply()

    assert list(export_dir.glob("*.tmp")) == []


# ---------- N4: deleting old export files ----------


def make_file(folder, name, age_hours):
    path = folder / name
    path.write_text("x")
    old = time.time() - age_hours * 3600
    os.utime(path, (old, old))
    return path


def test_old_files_are_deleted_and_new_ones_kept(export_dir, monkeypatch):
    monkeypatch.setattr(settings, "EXPORT_RETENTION_HOURS", 24)
    old = make_file(export_dir, "tasks-old.csv", 30)
    old_tmp = make_file(export_dir, "tasks-old.tmp", 30)
    new = make_file(export_dir, "tasks-new.csv", 1)
    other = make_file(export_dir, "notes.txt", 30)

    assert delete_old_exports() == 2

    assert not old.exists()
    assert not old_tmp.exists()
    assert new.exists()
    assert other.exists()  # only tasks-*.csv and tasks-*.tmp are touched


def test_cleanup_with_a_missing_folder_deletes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "EXPORT_DIR", str(tmp_path / "missing"))

    assert delete_old_exports() == 0


def test_cleanup_job_is_registered_and_scheduled():
    entries = [e["task"] for e in celery_app.conf.beat_schedule.values()]

    assert "app.celery_jobs.cleanup_exports" in celery_app.tasks
    assert "app.celery_jobs.cleanup_exports" in entries
