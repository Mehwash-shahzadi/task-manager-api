"""A slow reader must not store an old row after an update cleared the cache."""

from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from app import cache
from app.cache import GENERATION_KEY, LIST_KEY, task_key
from app.redis_client import get_cache_client
from tests.fakes import FakeCache


class RaceCache(FakeCache):
    """A cache where a write (an update) happens right after the reader notes the token."""

    def token(self):
        value = self.generation
        self.invalidate_tasks()  # The update commits and clears the cache here.
        return value


@pytest.fixture
def race_cache(client):
    from app.cache import get_task_cache
    from app.main import app

    fake = RaceCache()
    app.dependency_overrides[get_task_cache] = lambda: fake
    return fake


def test_a_reader_that_started_before_an_update_does_not_store_the_old_task(
    client, race_cache
):
    task_id = client.post("/v1/tasks", json={"title": "a"}).json()["data"]["id"]
    race_cache.store.clear()

    response = client.get(f"/v1/tasks/{task_id}")  # Reads the DB, then tries to cache.

    assert response.status_code == 200  # The client still gets its answer...
    assert (
        task_key(task_id) not in race_cache.store
    )  # ...but the old row is not cached.


def test_the_same_for_the_list(client, race_cache):
    client.get("/v1/tasks")

    assert LIST_KEY not in race_cache.store


def test_the_same_for_a_remembered_404(client, race_cache):
    task_id = uuid4()

    assert client.get(f"/v1/tasks/{task_id}").status_code == 404

    assert task_key(task_id) not in race_cache.store


def test_without_a_concurrent_update_the_task_is_cached(client, fake_cache):
    task_id = client.post("/v1/tasks", json={"title": "a"}).json()["data"]["id"]

    client.get(f"/v1/tasks/{task_id}")

    assert fake_cache.store[task_key(task_id)]["title"] == "a"


# ---- the real Redis functions (needs the cache Redis container, like the other tests) ----


@pytest.fixture
def real_cache():
    redis = get_cache_client()
    try:
        redis.ping()
    except RedisError:
        pytest.skip("cache Redis is not running")
    key = f"{cache.PREFIX}test-race-{uuid4()}"
    yield key
    redis.delete(key)


def test_a_write_with_the_current_token_is_stored(real_cache):
    token = cache.cache_token()

    cache.cache_set(real_cache, {"a": 1}, 30, token)

    assert cache.cache_get(real_cache) == {"a": 1}


def test_a_write_with_an_old_token_is_refused(real_cache):
    token = cache.cache_token()
    cache.invalidate_tasks()  # An update happens.

    cache.cache_set(real_cache, {"old": True}, 30, token)

    assert cache.cache_get(real_cache) is None


def test_each_clear_raises_the_generation():
    before = cache.cache_token()

    cache.invalidate_tasks()
    cache.invalidate_tasks()

    assert cache.cache_token() == before + 2


def test_the_generation_key_is_not_listed_as_a_cache_entry(real_cache):
    cache.invalidate_tasks()
    cache.cache_set(real_cache, {"a": 1}, 30)

    keys = [entry["key"] for entry in cache.cache_list()]

    assert real_cache in keys
    assert GENERATION_KEY not in keys


def test_a_write_without_a_token_still_works(real_cache):
    cache.cache_set(real_cache, {"a": 1}, 30)

    assert cache.cache_get(real_cache) == {"a": 1}


def test_when_redis_is_down_the_token_is_zero_and_nothing_breaks(monkeypatch):
    class Down:
        def get(self, key):
            raise RedisError("down")

        def eval(self, *args):
            raise RedisError("down")

        def incr(self, key):
            raise RedisError("down")

        def delete(self, *keys):
            raise RedisError("down")

    monkeypatch.setattr(cache, "get_cache_client", Down)

    assert cache.cache_token() == 0
    cache.cache_set("k", {"a": 1}, 30, 0)  # No error.
    cache.invalidate_tasks()  # No error.
