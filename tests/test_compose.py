"""Checks the Celery services in docker-compose.yml (Task 3). No Docker needed."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CELERY_SERVICES = ["celery-worker", "celery-beat", "flower"]


@pytest.fixture(autouse=True)
def clean_tables():
    """These tests do not touch the database, so skip the conftest cleanup."""
    yield


@pytest.fixture(scope="module")
def services():
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())
    return compose["services"]


@pytest.mark.parametrize("name", CELERY_SERVICES)
def test_service_exists(services, name):
    assert name in services


@pytest.mark.parametrize("name", CELERY_SERVICES)
def test_service_uses_the_same_image_and_env_file_as_the_api(services, name):
    assert services[name]["build"] == "."
    assert services[name]["env_file"] == ".env"


@pytest.mark.parametrize("name", CELERY_SERVICES)
def test_service_talks_to_redis_and_db_by_service_name(services, name):
    env = services[name]["environment"]

    assert env["REDIS_URL"] == "redis://:${REDIS_PASSWORD}@redis:6379/0"
    assert env["CELERY_BROKER_URL"] == "redis://:${REDIS_PASSWORD}@redis:6379/1"
    assert env["CELERY_RESULT_BACKEND"] == "redis://:${REDIS_PASSWORD}@redis:6379/2"
    assert env["POSTGRES_HOST"] == "db"
    assert env["POSTGRES_PORT"] == "5432"
    assert "localhost" not in " ".join(env.values())


@pytest.mark.parametrize("name", CELERY_SERVICES)
def test_service_waits_for_healthy_db_and_redis(services, name):
    depends_on = services[name]["depends_on"]

    assert depends_on["db"]["condition"] == "service_healthy"
    assert depends_on["redis"]["condition"] == "service_healthy"


@pytest.mark.parametrize("name", CELERY_SERVICES)
def test_service_restarts_unless_stopped(services, name):
    assert services[name]["restart"] == "unless-stopped"


def test_worker_command(services):
    entrypoint = services["celery-worker"]["entrypoint"]

    assert entrypoint[:5] == ["celery", "-A", "app.celery_app", "worker", "-l"]


def test_worker_gets_time_to_finish_running_jobs(services):
    assert services["celery-worker"]["stop_grace_period"] == "30s"


def test_beat_command_keeps_its_schedule_file_in_tmp(services):
    entrypoint = services["celery-beat"]["entrypoint"]

    assert entrypoint[:4] == ["celery", "-A", "app.celery_app", "beat"]
    assert entrypoint[entrypoint.index("--schedule") + 1] == "/tmp/celerybeat-schedule"


def test_there_is_exactly_one_beat_and_it_is_not_scaled(services):
    # two Beat processes would schedule every job twice
    beats = [n for n, s in services.items() if "beat" in s.get("entrypoint", [])]

    assert beats == ["celery-beat"]
    assert "deploy" not in services["celery-beat"]
    assert "scale" not in services["celery-beat"]


def test_flower_command_and_port(services):
    flower = services["flower"]

    assert flower["entrypoint"][:4] == ["celery", "-A", "app.celery_app", "flower"]
    assert "--port=5555" in flower["entrypoint"]
    assert "127.0.0.1:5555:5555" in flower["ports"]


def test_only_the_api_and_flower_publish_new_ports(services):
    assert "ports" not in services["celery-worker"]
    assert "ports" not in services["celery-beat"]


def test_the_old_stream_worker_is_unchanged(services):
    worker = services["stream-worker"]

    assert worker["entrypoint"] == ["python", "-m", "app.messaging.worker"]
    assert "CELERY_BROKER_URL" not in worker["environment"]


def test_beat_schedule_file_is_git_ignored():
    assert (
        "celerybeat-schedule*" in (PROJECT_ROOT / ".gitignore").read_text().splitlines()
    )


# ---------- Task 8: shared exports volume ----------


def test_exports_volume_is_declared():
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())

    assert "exports" in compose["volumes"]


@pytest.mark.parametrize("name", ["app", "celery-worker"])
def test_api_and_celery_worker_mount_the_same_exports_volume(services, name):
    assert "exports:/code/exports" in services[name]["volumes"]


@pytest.mark.parametrize(
    "name", ["celery-beat", "flower", "stream-worker", "db", "redis"]
)
def test_other_services_do_not_mount_exports(services, name):
    assert not any("exports" in v for v in services[name].get("volumes", []))


def test_mount_path_matches_the_export_dir_in_env_example():
    # EXPORT_DIR is the relative path "exports" and the image WORKDIR is /code
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()

    assert "WORKDIR /code" in dockerfile
    assert (
        "EXPORT_DIR=exports" in (PROJECT_ROOT / ".env.example").read_text().splitlines()
    )


@pytest.mark.parametrize("filename", [".gitignore", ".dockerignore"])
def test_exports_folder_is_ignored(filename):
    lines = (PROJECT_ROOT / filename).read_text().splitlines()

    assert "exports/" in lines


# ---------- Security: ports on localhost only, Redis password, Flower login ----------


def test_db_redis_and_flower_ports_are_bound_to_localhost_only(services):
    for name in ["db", "redis", "flower"]:
        for port in services[name]["ports"]:
            assert str(port).startswith("127.0.0.1:"), (
                f"{name} publishes {port} on all interfaces"
            )


def test_redis_requires_a_password(services):
    command = services["redis"]["command"]

    assert "--requirepass" in command
    assert "${REDIS_PASSWORD}" in command


def test_redis_healthcheck_sends_the_password(services):
    test = " ".join(services["redis"]["healthcheck"]["test"])

    assert "REDISCLI_AUTH" in test


@pytest.mark.parametrize(
    "name", ["app", "stream-worker", "celery-worker", "celery-beat", "flower"]
)
def test_every_redis_url_in_compose_has_the_password(services, name):
    env = services[name]["environment"]

    for key, value in env.items():
        if key == "REDIS_URL" or key.startswith("CELERY_"):
            assert value.startswith("redis://:${REDIS_PASSWORD}@redis:"), (
                f"{name}.{key} has no password"
            )


def test_env_example_documents_the_new_secrets():
    keys = {
        line.split("=", 1)[0]
        for line in (PROJECT_ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    }

    assert {"REDIS_PASSWORD", "FLOWER_BASIC_AUTH"} <= keys


def test_env_example_has_no_duplicate_keys():
    keys = [
        line.split("=", 1)[0]
        for line in (PROJECT_ROOT / ".env.example").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    ]

    assert len(keys) == len(set(keys))


def test_api_restarts_and_has_a_healthcheck(services):
    api = services["app"]

    assert api["restart"] == "unless-stopped"
    assert "/health" in " ".join(api["healthcheck"]["test"])


def test_redis_keeps_its_data_on_disk(services):
    redis = services["redis"]

    assert "--appendonly" in redis["command"]
    assert "yes" in redis["command"]
    assert "redisdata:/data" in redis["volumes"]


def test_redis_volume_is_declared():
    import yaml as yaml_module

    compose = yaml_module.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())

    assert "redisdata" in compose["volumes"]


# ---------- the cache Redis has its own password ----------


def test_the_cache_redis_uses_its_own_password(services):
    command = services["redis-cache"]["command"]

    assert "${CACHE_REDIS_PASSWORD}" in command
    assert "${REDIS_PASSWORD}" not in command
    assert (
        services["redis-cache"]["environment"]["REDIS_PASSWORD"]
        == "${CACHE_REDIS_PASSWORD}"
    )


def test_the_main_redis_does_not_know_the_cache_password(services):
    assert "CACHE_REDIS_PASSWORD" not in " ".join(services["redis"]["command"])


@pytest.mark.parametrize("name", ["app", "celery-worker", "celery-beat", "flower"])
def test_the_cache_url_in_compose_uses_the_cache_password(services, name):
    env = services[name]["environment"]

    assert env["CACHE_REDIS_URL"].startswith(
        "redis://:${CACHE_REDIS_PASSWORD}@redis-cache:"
    )


def test_the_two_passwords_differ_in_env_example():
    text = (PROJECT_ROOT / ".env.example").read_text()
    values = dict(
        line.split("=", 1)
        for line in text.splitlines()
        if "=" in line and not line.startswith("#")
    )

    assert values["REDIS_PASSWORD"] != values["CACHE_REDIS_PASSWORD"]
    assert values["CACHE_REDIS_PASSWORD"] in values["CACHE_REDIS_URL"]
    assert values["REDIS_PASSWORD"] not in values["CACHE_REDIS_URL"]
