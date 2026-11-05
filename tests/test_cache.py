"""Cache: keys, lifetimes, hit/miss numbers, injection and the cached 404."""

import logging
from uuid import uuid4

from app import cache
from app.cache import LIST_KEY, PREFIX, TaskCache, get_task_cache, task_key
from app.config import settings
from app.main import app
from tests.fakes import FakeCacheRedis


def create(client, key=None, title="a"):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/v1/tasks", json={"title": title}, headers=headers)


def test_invalidate_tasks_clears_the_list_and_each_task(monkeypatch):
    calls = []
    monkeypatch.setattr("app.cache.cache_invalidate", lambda *keys: calls.append(keys))
    a, b = uuid4(), uuid4()

    cache.invalidate_tasks(a, b)
    cache.invalidate_tasks()

    assert calls == [(LIST_KEY, task_key(a), task_key(b)), (LIST_KEY,)]


# ---------- B8: /cache uses three calls, not two per key ----------


def test_cache_list_makes_a_fixed_number_of_calls(monkeypatch):
    fake = FakeCacheRedis({f"{PREFIX}{i}": '{"n": %d}' % i for i in range(20)})
    monkeypatch.setattr(cache, "get_cache_client", lambda: fake)

    entries = cache.cache_list()

    assert len(entries) == 20
    assert entries[0] == {"key": f"{PREFIX}0", "ttl": 30, "value": {"n": 0}}
    assert sorted(fake.calls) == ["mget", "pipeline", "scan"]


def test_cache_list_skips_expired_and_broken_values(monkeypatch, caplog):
    fake = FakeCacheRedis(
        {f"{PREFIX}a": None, f"{PREFIX}b": "not json", f"{PREFIX}c": "1"}
    )
    monkeypatch.setattr(cache, "get_cache_client", lambda: fake)

    with caplog.at_level(logging.WARNING, logger="app.cache"):
        entries = cache.cache_list()

    assert [e["key"] for e in entries] == [f"{PREFIX}c"]
    assert "Bad cached value" in caplog.text


def test_cache_list_with_no_keys_makes_one_call(monkeypatch):
    fake = FakeCacheRedis({})
    monkeypatch.setattr(cache, "get_cache_client", lambda: fake)

    assert cache.cache_list() == []
    assert fake.calls == ["scan"]


# ---------- B9: requirements are pinned ----------


def test_cache_keys_carry_the_cache_version():
    from uuid import uuid4

    assert f"{cache.settings.KEY_PREFIX}tasks:v{cache.CACHE_VERSION}:" == cache.PREFIX
    assert cache.LIST_KEY.startswith(cache.PREFIX)
    assert cache.task_key(uuid4()).startswith(cache.PREFIX)


# ---------- I3: the cache comes in through Depends ----------


def test_get_task_reads_from_the_injected_cache_without_the_database(
    client, fake_cache
):
    task_id = uuid4()
    cached = {
        "id": str(task_id),
        "title": "only in the cache",
        "description": None,
        "status": "pending",
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-01T00:00:00Z",
    }
    fake_cache.store[task_key(task_id)] = cached

    response = client.get(f"/v1/tasks/{task_id}")  # No such row in the database.

    assert response.status_code == 200
    assert response.json()["data"]["title"] == "only in the cache"


def test_list_is_stored_in_the_injected_cache_and_create_clears_it(client, fake_cache):
    client.get("/v1/tasks")
    assert LIST_KEY in fake_cache.store

    client.post("/v1/tasks", json={"title": "new"})

    assert LIST_KEY not in fake_cache.store


def test_update_and_delete_clear_the_cached_copy(client, fake_cache):
    task_id = client.post("/v1/tasks", json={"title": "a"}).json()["data"]["id"]
    client.get(f"/v1/tasks/{task_id}")
    assert task_key(task_id) in fake_cache.store

    client.patch(f"/v1/tasks/{task_id}", json={"title": "b"})
    assert task_key(task_id) not in fake_cache.store

    client.get(f"/v1/tasks/{task_id}")
    client.delete(f"/v1/tasks/{task_id}")
    assert task_key(task_id) not in fake_cache.store


def test_the_real_dependency_returns_a_task_cache():
    assert isinstance(get_task_cache(), TaskCache)


def test_the_router_no_longer_imports_the_cache_functions():
    import app.routers.tasks as router

    for name in ("cache_get", "cache_set", "invalidate_tasks"):
        assert not hasattr(router, name)


# ---------- M18: the list and one task have their own cache time ----------


def test_list_and_task_are_cached_with_their_own_ttl(client, monkeypatch):
    seen = {}

    class SpyCache:
        def get(self, key):
            return None

        def token(self):
            return 0

        def set(self, key, value, ttl, token=None):
            seen[key] = ttl

        def invalidate_tasks(self, *ids):
            pass

    app.dependency_overrides[get_task_cache] = SpyCache
    monkeypatch.setattr(settings, "LIST_CACHE_TTL", 11)
    monkeypatch.setattr(settings, "TASK_CACHE_TTL", 22)
    task_id = create(client).json()["data"]["id"]

    client.get("/v1/tasks")
    client.get(f"/v1/tasks/{task_id}")

    assert seen == {LIST_KEY: 11, task_key(task_id): 22}


def test_cache_set_passes_the_ttl_to_redis(monkeypatch):
    from app import cache

    calls = []

    class Spy:
        def set(self, key, value, ex=None):
            calls.append((key, ex))

    monkeypatch.setattr(cache, "get_cache_client", Spy)

    cache.cache_set("k", {"a": 1}, 7)

    assert calls == [("k", 7)]


def test_there_is_no_single_cache_ttl_setting_any_more():
    fields = type(settings).model_fields
    assert "CACHE_TTL" not in fields
    assert {"LIST_CACHE_TTL", "TASK_CACHE_TTL"} <= set(fields)


# ---------- M20: cache hit and miss numbers ----------


def test_cache_get_counts_hits_and_misses(monkeypatch):
    from app import cache

    values = {"good": '{"a": 1}', "bad": "not json"}

    class Redis:
        def get(self, key):
            return values.get(key)

        def delete(self, *keys):
            pass

    monkeypatch.setattr(cache, "get_cache_client", Redis)
    before = cache.cache_stats()

    assert cache.cache_get("good") == {"a": 1}
    assert cache.cache_get("missing") is None
    assert cache.cache_get("bad") is None  # A broken value counts as a miss.

    after = cache.cache_stats()
    assert after["hits"] - before["hits"] == 1
    assert after["misses"] - before["misses"] == 2


def test_redis_down_counts_as_a_miss(monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    from app import cache

    class Down:
        def get(self, key):
            raise RedisConnectionError("down")

    monkeypatch.setattr(cache, "get_cache_client", Down)
    before = cache.cache_stats()["misses"]

    assert cache.cache_get("k") is None

    assert cache.cache_stats()["misses"] == before + 1


def test_cache_stats_returns_a_copy():
    from app import cache

    cache.cache_stats()["hits"] = 10**9

    assert cache.cache_stats()["hits"] != 10**9


def test_health_shows_the_cache_numbers(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert set(response.json()["cache"]) == {"hits", "misses"}


# ---------- M17: every key starts with KEY_PREFIX ----------


def test_cache_job_and_seen_keys_all_start_with_the_prefix(monkeypatch):
    from app import cache, job_registry
    from app.messaging import consumer

    prefix = settings.KEY_PREFIX
    assert prefix  # Not empty, or the prefix would do nothing.
    assert cache.PREFIX.startswith(prefix)
    assert cache.LIST_KEY.startswith(prefix)
    assert task_key(uuid4()).startswith(prefix)
    assert job_registry.JOB_PREFIX.startswith(prefix)
    assert consumer.seen_key("e1").startswith(prefix)


# ---------- L11: a 404 is cached for a few seconds ----------


def test_an_unknown_task_is_remembered_and_the_second_request_skips_the_database(
    client, session, fake_cache, monkeypatch
):
    from app.routers.tasks import NOT_FOUND

    task_id = uuid4()
    assert client.get(f"/v1/tasks/{task_id}").status_code == 404
    assert fake_cache.store[task_key(task_id)] == NOT_FOUND

    def boom(*args, **kwargs):
        raise AssertionError("the database must not be asked again")

    monkeypatch.setattr(session, "get", boom)

    assert client.get(f"/v1/tasks/{task_id}").status_code == 404


def test_the_not_found_marker_uses_its_own_ttl(client, monkeypatch):
    seen = {}

    class SpyCache:
        def get(self, key):
            return None

        def token(self):
            return 0

        def set(self, key, value, ttl, token=None):
            seen[key] = ttl

        def invalidate_tasks(self, *ids):
            pass

    app.dependency_overrides[get_task_cache] = SpyCache
    monkeypatch.setattr(settings, "NOT_FOUND_CACHE_TTL", 7)
    task_id = uuid4()

    client.get(f"/v1/tasks/{task_id}")

    assert seen == {task_key(task_id): 7}


def test_delete_clears_a_remembered_404(client, fake_cache):
    task_id = uuid4()
    client.get(f"/v1/tasks/{task_id}")
    assert task_key(task_id) in fake_cache.store

    client.delete(f"/v1/tasks/{task_id}")

    assert task_key(task_id) not in fake_cache.store


def test_a_real_task_is_still_cached_as_the_task(client, fake_cache):
    task_id = create(client).json()["data"]["id"]

    client.get(f"/v1/tasks/{task_id}")

    assert fake_cache.store[task_key(task_id)]["title"] == "a"
