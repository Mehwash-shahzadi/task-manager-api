"""The image is built from the pinned lock file, and the containers run with less power."""

import re
from importlib.metadata import version
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEV_ONLY = {"pytest", "ruff", "mypy", "httpx2", "pip-audit", "iniconfig", "pluggy"}
OUR_IMAGE = ["app", "stream-worker", "celery-worker", "celery-beat", "flower"]


def lock_pins():
    pins = {}
    for line in (PROJECT_ROOT / "requirements.lock").read_text().splitlines():
        if line and not line.startswith("#"):
            name, _, pinned = line.partition("==")
            pins[name] = pinned
    return pins


def top_level_names():
    lines = (PROJECT_ROOT / "requirements.txt").read_text().split()
    return {re.split(r"[\[=]", line)[0].lower().replace("_", "-") for line in lines}


@pytest.fixture(scope="module")
def dockerfile():
    return (PROJECT_ROOT / "Dockerfile").read_text()


@pytest.fixture(scope="module")
def services():
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())["services"]


def test_the_image_installs_from_the_lock_file(dockerfile):
    assert "COPY requirements.lock" in dockerfile
    assert "pip install --no-cache-dir -r requirements.lock" in dockerfile
    assert "requirements.txt" not in dockerfile


def test_the_base_image_is_pinned_by_digest(dockerfile):
    assert re.search(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$", dockerfile, re.M)


def test_every_lock_line_is_an_exact_pin():
    for line in (PROJECT_ROOT / "requirements.lock").read_text().splitlines():
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[a-z0-9.\-]+==[0-9][\w.]*", line), line


def test_the_lock_has_every_top_level_package():
    assert top_level_names() <= set(lock_pins())


def test_the_lock_has_no_dev_tools():
    assert DEV_ONLY.isdisjoint(lock_pins())


def test_the_lock_has_the_versions_the_tests_run_with():
    for name, pinned in lock_pins().items():
        assert version(name) == pinned, name


def test_the_app_user_cannot_change_the_code(dockerfile):
    assert "chown -R" not in dockerfile  # The code stays owned by root.
    assert "chown appuser:appuser /code/exports" in dockerfile
    assert "USER appuser" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile


@pytest.mark.parametrize("name", OUR_IMAGE)
def test_our_containers_run_hardened(services, name):
    service = services[name]

    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["read_only"] is True
    assert "/tmp" in service["tmpfs"]


def test_the_beat_schedule_file_is_in_the_writable_tmp(services):
    entrypoint = services["celery-beat"]["entrypoint"]

    assert entrypoint[entrypoint.index("--schedule") + 1].startswith("/tmp/")


def test_the_export_volume_stays_writable(services):
    assert "exports:/code/exports" in services["app"]["volumes"]
    assert "exports:/code/exports" in services["celery-worker"]["volumes"]


def test_plan_md_says_it_is_history_and_points_to_the_readme():
    text = (PROJECT_ROOT / "PLAN.md").read_text()

    assert "original plan" in text.split("**Goal:**")[0]
    assert "README is the current truth" in text


# ---------- M26: uvicorn trusts proxy headers only from listed proxies ----------


def test_entrypoint_turns_on_proxy_headers_for_listed_proxies_only():
    script = (PROJECT_ROOT / "entrypoint.sh").read_text()

    assert "--proxy-headers" in script
    assert '--forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"' in script
    assert '--forwarded-allow-ips "*"' not in script


def test_env_example_lists_forwarded_allow_ips():
    text = (PROJECT_ROOT / ".env.example").read_text()

    assert "FORWARDED_ALLOW_IPS=" in text
