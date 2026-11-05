"""Where the code lives: package layout and one place for the test fakes."""

import importlib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_jobs_module_replaced_app_tasks():
    assert importlib.import_module("app.celery_jobs")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.tasks")


# ---------- E3: the messaging files live in one package ----------


def test_messaging_files_moved_into_the_package():
    app_dir = PROJECT_ROOT / "app"

    for name in ("publisher", "monitor", "outbox", "consumer", "worker", "replay"):
        assert (app_dir / "messaging" / f"{name}.py").exists()
        assert not (app_dir / f"{name}.py").exists()  # No leftover copy.


def test_the_stream_worker_container_starts_the_moved_module():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()

    assert "app.messaging.worker" in compose
    assert '"app.worker"' not in compose


# ---------- I20: the fakes live in one file ----------


def test_fake_classes_are_defined_only_in_tests_fakes_py():
    import re

    for path in (PROJECT_ROOT / "tests").glob("*.py"):
        if path.name in ("fakes.py", "test_project_layout.py"):
            continue
        text = path.read_text()
        assert not re.search(r"^class (Fake|\w*Redis)\w*", text, re.M), path.name
