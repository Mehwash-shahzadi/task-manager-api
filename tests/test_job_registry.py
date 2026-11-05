"""The export lock: only one export runs at a time, and it is always given back."""

from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from app import job_registry
from app.redis_client import get_redis_client


@pytest.fixture
def lock_key(monkeypatch):
    """A private lock key, so tests never touch the real one."""
    key = f"{job_registry.EXPORT_LOCK_KEY}-test-{uuid4()}"
    monkeypatch.setattr(job_registry, "EXPORT_LOCK_KEY", key)
    try:
        get_redis_client().ping()
    except RedisError:
        pytest.skip("Redis is not running")
    yield key
    get_redis_client().delete(key)


def test_the_first_export_gets_the_lock(lock_key):
    assert job_registry.claim_export("job-1") is None
    assert get_redis_client().get(lock_key) == "job-1"


def test_a_second_export_is_told_who_holds_the_lock(lock_key):
    job_registry.claim_export("job-1")

    assert job_registry.claim_export("job-2") == "job-1"
    assert get_redis_client().get(lock_key) == "job-1"  # Not overwritten.


def test_after_release_the_next_export_can_start(lock_key):
    job_registry.claim_export("job-1")

    job_registry.release_export("job-1")

    assert job_registry.claim_export("job-2") is None


def test_release_does_not_free_a_lock_that_belongs_to_another_job(lock_key):
    job_registry.claim_export("job-1")

    job_registry.release_export("job-2")  # Not the holder.

    assert get_redis_client().get(lock_key) == "job-1"


def test_the_lock_expires_by_itself(lock_key):
    job_registry.claim_export("job-1")

    ttl = get_redis_client().ttl(lock_key)

    assert 0 < ttl <= job_registry.settings.EXPORT_LOCK_SECONDS


def test_release_and_forget_never_raise_when_redis_is_down(monkeypatch):
    class Down:
        def eval(self, *args):
            raise RedisError("down")

        def delete(self, *args):
            raise RedisError("down")

    monkeypatch.setattr(job_registry, "get_redis_client", Down)

    job_registry.release_export("job-1")  # No error.
    job_registry.forget_job("job-1")  # No error.


def test_claim_raises_when_redis_is_down(monkeypatch):
    class Down:
        def set(self, *args, **kwargs):
            raise RedisError("down")

    monkeypatch.setattr(job_registry, "get_redis_client", Down)

    with pytest.raises(RedisError):
        job_registry.claim_export("job-1")


def test_the_export_job_gives_the_lock_back_when_it_finishes_or_fails(
    lock_key, tmp_path, monkeypatch
):
    from app import celery_jobs as jobs

    monkeypatch.setattr(jobs.settings, "EXPORT_DIR", str(tmp_path))
    job_registry.claim_export("job-ok")
    jobs.export_tasks_csv.apply(task_id="job-ok")
    assert get_redis_client().get(lock_key) is None

    job_registry.claim_export("job-bad")
    monkeypatch.setattr(jobs, "write_export", lambda job_id: 1 / 0)
    result = jobs.export_tasks_csv.apply(task_id="job-bad")
    assert result.failed()
    assert get_redis_client().get(lock_key) is None
