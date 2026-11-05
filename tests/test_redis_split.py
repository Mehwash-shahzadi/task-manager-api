"""L13 (stream backlog warning) and M15 (separate Redis servers for cache and streams)."""

import logging
from pathlib import Path

import pytest
from redis.exceptions import RedisError, ResponseError

from app import cache, job_registry, redis_client
from app.config import settings
from app.messaging import consumer, monitor
from app.redis_client import make_redis
from tests.fakes import GroupsRedis

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------- L13: stream backlog ----------


def group(lag, pending, name=None):
    return {"name": name or settings.CONSUMER_GROUP, "lag": lag, "pending": pending}


def test_backlog_is_unread_plus_unacknowledged(monkeypatch):
    monkeypatch.setattr(monitor, "get_redis_client", lambda: GroupsRedis([group(5, 2)]))

    assert monitor.stream_backlog() == 7


@pytest.mark.parametrize(
    "fake",
    [
        GroupsRedis([group(None, 2)]),  # Redis could not work out the lag
        GroupsRedis([group(5, 2, name="another-group")]),
        GroupsRedis([]),
        GroupsRedis(error=RedisError("down")),
        GroupsRedis(error=ResponseError("no such key")),
    ],
)
def test_backlog_is_none_when_redis_cannot_tell(monkeypatch, fake):
    monkeypatch.setattr(monitor, "get_redis_client", lambda: fake)

    assert monitor.stream_backlog() is None


def test_error_is_logged_when_the_stream_is_nearly_full(monkeypatch, caplog):
    monkeypatch.setattr(settings, "EVENT_STREAM_MAXLEN", 1000)
    monkeypatch.setattr(monitor, "stream_backlog", lambda *args: 800)

    with caplog.at_level(logging.ERROR):
        assert monitor.check_stream_backlog() == 800

    assert "nearly full" in caplog.text


@pytest.mark.parametrize("backlog", [0, 799, None])
def test_no_error_below_the_warning_level(monkeypatch, caplog, backlog):
    monkeypatch.setattr(settings, "EVENT_STREAM_MAXLEN", 1000)
    monkeypatch.setattr(monitor, "stream_backlog", lambda *args: backlog)

    with caplog.at_level(logging.ERROR):
        monitor.check_stream_backlog()

    assert caplog.text == ""


def test_watch_stream_runs_every_minute_and_expires():
    from app.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["watch-stream"]

    assert entry["task"] == "app.celery_jobs.watch_stream"
    assert entry["schedule"] == 60.0
    assert entry["options"]["expires"] < entry["schedule"]


def test_stream_limit_in_the_example_file_is_generous():
    text = (PROJECT_ROOT / ".env.example").read_text()

    limit = int(
        next(
            line.split("=")[1]
            for line in text.splitlines()
            if line.startswith("EVENT_STREAM_MAXLEN=")
        )
    )
    assert limit >= 100_000


# ---------- M15: two Redis servers ----------


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return None

        return call


def test_cache_keys_go_to_the_cache_server_only(monkeypatch):
    cache_server, main_server = Recorder(), Recorder()
    monkeypatch.setattr(cache, "get_cache_client", lambda: cache_server)
    monkeypatch.setattr(job_registry, "get_redis_client", lambda: main_server)

    cache.cache_get("k")
    cache.cache_set("k", {"a": 1}, 60)
    cache.invalidate_tasks()

    assert cache_server.calls == ["get", "set", "incr", "delete"]
    assert main_server.calls == []


def test_job_ids_go_to_the_main_server_that_never_evicts(monkeypatch):
    cache_server, main_server = Recorder(), Recorder()
    monkeypatch.setattr(cache, "get_cache_client", lambda: cache_server)
    monkeypatch.setattr(job_registry, "get_redis_client", lambda: main_server)

    job_registry.remember_job("job-1")
    job_registry.job_exists("job-1")

    assert main_server.calls == ["set", "exists"]
    assert cache_server.calls == []


def test_the_two_clients_point_at_different_servers():
    assert redis_client.get_cache_client() is not redis_client.get_redis_client()
    assert settings.CACHE_REDIS_URL != settings.REDIS_URL


def test_make_redis_uses_the_url_it_is_given():
    client = make_redis(1.0, "redis://:pw@cache-host:6380/3")
    kwargs = client.connection_pool.connection_kwargs

    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("cache-host", 6380, 3)


def test_the_outbox_writes_to_the_main_server():
    from app.messaging import outbox

    assert outbox.get_redis_client is redis_client.get_redis_client


# ---------- M15: docker-compose.yml ----------

yaml = pytest.importorskip("yaml")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())


def flags(service):
    return " ".join(service["command"])


def test_cache_server_may_drop_old_keys_and_has_no_disk(compose):
    command = flags(compose["services"]["redis-cache"])

    assert "--maxmemory-policy allkeys-lru" in command
    assert "--maxmemory 128mb" in command
    assert "--appendonly no" in command
    assert "volumes" not in compose["services"]["redis-cache"]


def test_main_server_never_drops_data_and_keeps_its_disk(compose):
    command = flags(compose["services"]["redis"])

    assert "--maxmemory-policy noeviction" in command
    assert "--maxmemory " in command
    assert "--appendonly yes" in command


def test_app_uses_the_cache_server_by_service_name(compose):
    app = compose["services"]["app"]

    assert app["environment"]["CACHE_REDIS_URL"].endswith("@redis-cache:6379/0")
    assert app["depends_on"]["redis-cache"]["condition"] == "service_healthy"


def test_cache_server_is_published_on_its_own_local_port(compose):
    assert compose["services"]["redis-cache"]["ports"] == ["127.0.0.1:6380:6379"]


# ---------- M19: one place builds Redis clients ----------


def test_make_redis_sets_health_check_and_limit():
    r = make_redis(7.5)
    pool = r.connection_pool

    assert pool.max_connections == settings.REDIS_MAX_CONNECTIONS
    assert pool.connection_kwargs["health_check_interval"] == 30
    assert pool.connection_kwargs["socket_timeout"] == 7.5
    assert pool.connection_kwargs["socket_connect_timeout"] == settings.REDIS_TIMEOUT


def test_both_clients_use_the_factory():
    from app.redis_client import get_cache_client, get_redis_client

    for r in (get_cache_client(), get_redis_client(), consumer.get_consumer_redis()):
        assert r.connection_pool.max_connections == settings.REDIS_MAX_CONNECTIONS
        assert r.connection_pool.connection_kwargs["health_check_interval"] == 30
