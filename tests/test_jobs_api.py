"""Task 9: POST /tasks/export and GET /jobs/{job_id}. Fakes replace Celery, so no worker is needed."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError

from app import job_registry
from app.main import app
from tests.fakes import FakeJob

PROJECT_ROOT = Path(__file__).resolve().parent.parent

client = TestClient(app)

JOB_ID = "69fcb823-30ac-4847-8bdb-2a9c5a4be3e6"


# ---------- fakes ----------


def fake_async_result(monkeypatch, status, result=None, traceback=None):
    """Replaces AsyncResult in the jobs router. Returns the ids it was asked for."""
    asked = []

    class FakeAsyncResult:
        def __init__(self, job_id, app=None):
            asked.append(job_id)
            self.status = status
            self.result = result
            self.traceback = traceback

    monkeypatch.setattr("app.routers.jobs.AsyncResult", FakeAsyncResult)
    monkeypatch.setattr("app.routers.jobs.job_exists", lambda job_id: True)
    return asked


def fake_registry(monkeypatch):
    """The job registry and the export lock use Redis; these tests use no Redis."""
    monkeypatch.setattr("app.routers.tasks.claim_export", lambda job_id: None)
    monkeypatch.setattr("app.routers.tasks.remember_job", lambda job_id: None)
    monkeypatch.setattr("app.routers.tasks.forget_job", lambda job_id: None)
    monkeypatch.setattr("app.routers.tasks.release_export", lambda job_id: None)


@pytest.fixture
def fake_job(monkeypatch):
    job = FakeJob()
    monkeypatch.setattr("app.routers.tasks.export_tasks_csv", job)
    fake_registry(monkeypatch)
    return job


# ---------- POST /tasks/export ----------


def test_export_returns_202_and_the_job_id(fake_job):
    response = client.post("/v1/tasks/export")

    assert response.status_code == 202
    body = response.json()
    assert body["success"] is True
    assert body["message"] == "Export started"
    assert body["data"]["job_id"] == fake_job.calls[0]  # Queued under the id we return.
    assert body["data"]["status"] == "PENDING"
    assert body["errors"] is None


def test_export_starts_the_job_exactly_once(fake_job):
    client.post("/v1/tasks/export")

    assert len(fake_job.calls) == 1


def test_export_does_not_wait_for_the_job(fake_job):
    # no result is returned yet: the data holds the id and status, nothing else
    data = client.post("/v1/tasks/export").json()["data"]

    assert data["result"] is None
    assert data["error"] is None


def test_export_broker_down_gives_503_in_the_error_envelope(monkeypatch):
    monkeypatch.setattr(
        "app.routers.tasks.export_tasks_csv",
        FakeJob(error=OperationalError("broker down")),
    )
    fake_registry(monkeypatch)

    response = client.post("/v1/tasks/export")

    assert response.status_code == 503
    body = response.json()
    assert body["success"] is False
    assert body["message"] == "Job queue unavailable"
    assert body["errors"] == ["Job queue unavailable"]
    assert body["data"] is None


def test_export_route_is_not_swallowed_by_the_task_id_routes(fake_job):
    # POST /tasks/export must reach the export route, not PATCH/DELETE/GET /tasks/{id}
    assert client.post("/v1/tasks/export").status_code == 202


def test_get_tasks_export_is_still_read_as_a_task_id():
    # only POST /export exists; a GET falls through to GET /tasks/{id} and "export" is not a UUID
    response = client.get("/v1/tasks/export")

    assert response.status_code == 422
    assert response.json()["success"] is False


def test_export_with_redis_really_down_fails_fast_with_503():
    """The real Celery error, not a hand-made one.

    With Redis unreachable, .delay() raises a RuntimeError from the result backend
    (not kombu's OperationalError) and by default only after ~19 seconds.
    The endpoint should answer 503 quickly instead of hanging and returning 500.
    """
    script = (
        "import time\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app, raise_server_exceptions=False)\n"
        "t = time.time()\n"
        "r = c.post('/v1/tasks/export', headers={'X-API-Key': 'k'})\n"
        "print(r.status_code, round(time.time() - t, 1))\n"
    )
    env = {
        **os.environ,
        "API_KEY": "k",  # the script runs the real app, so it needs a key
        "CELERY_BROKER_URL": "redis://127.0.0.1:1/1",
        "CELERY_RESULT_BACKEND": "redis://127.0.0.1:1/2",
    }

    started = time.time()
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    elapsed = time.time() - started

    assert done.returncode == 0, done.stderr
    status = int(done.stdout.split()[-2])
    assert status == 503, f"got {status} after {elapsed:.0f}s (expected 503)"
    assert elapsed < 10, f"took {elapsed:.0f}s to fail (expected under 10s)"


# ---------- GET /jobs/{job_id} ----------


def test_pending_job(monkeypatch):
    fake_async_result(monkeypatch, "PENDING")

    response = client.get(f"/v1/jobs/{JOB_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["message"] == "Job status"
    assert body["data"] == {
        "job_id": JOB_ID,
        "status": "PENDING",
        "result": None,
        "error": None,
    }


def test_started_job(monkeypatch):
    fake_async_result(monkeypatch, "STARTED")

    data = client.get(f"/v1/jobs/{JOB_ID}").json()["data"]

    assert data["status"] == "STARTED"
    assert data["result"] is None
    assert data["error"] is None


def test_retrying_job_has_no_result_or_error(monkeypatch):
    fake_async_result(monkeypatch, "RETRY", result=RuntimeError("will try again"))

    data = client.get(f"/v1/jobs/{JOB_ID}").json()["data"]

    assert data["status"] == "RETRY"
    assert data["result"] is None
    assert data["error"] is None


def test_successful_job_returns_its_result(monkeypatch):
    fake_async_result(monkeypatch, "SUCCESS", result={"file": "tasks-x.csv", "rows": 3})

    data = client.get(f"/v1/jobs/{JOB_ID}").json()["data"]

    assert data["status"] == "SUCCESS"
    assert data["result"] == {"file": "tasks-x.csv", "rows": 3}
    assert data["error"] is None


def test_failed_job_returns_a_fixed_message_and_logs_the_real_error(
    monkeypatch, caplog
):
    fake_async_result(
        monkeypatch,
        "FAILURE",
        result=RuntimeError("db is down"),
        traceback='File "/code/app/celery_jobs.py", line 59, in export_tasks_csv',
    )

    response = client.get(f"/v1/jobs/{JOB_ID}")

    data = response.json()["data"]
    assert data["status"] == "FAILURE"
    assert data["error"] == "Job failed"  # Fixed text, not the real error.
    assert "db is down" not in response.text
    assert data["result"] is None
    assert "db is down" in caplog.text  # The real error is in the log.
    # internal paths must never reach the client
    assert "/code/app/celery_jobs.py" not in response.text
    assert "traceback" not in response.text.lower()


def test_job_id_is_passed_to_celery_unchanged(monkeypatch):
    asked = fake_async_result(monkeypatch, "PENDING")

    client.get("/v1/jobs/69fcb823-30ac-4847-8bdb-2a9c5a4be3e6")

    assert asked == ["69fcb823-30ac-4847-8bdb-2a9c5a4be3e6"]


def test_poll_goes_pending_started_success(monkeypatch):
    seen = []
    for status, result in [
        ("PENDING", None),
        ("STARTED", None),
        ("SUCCESS", {"file": "tasks-x.csv", "rows": 1}),
    ]:
        fake_async_result(monkeypatch, status, result=result)
        seen.append(client.get(f"/v1/jobs/{JOB_ID}").json()["data"]["status"])

    assert seen == ["PENDING", "STARTED", "SUCCESS"]


# ---------- wiring ----------


def test_both_routes_appear_in_the_api_docs():
    paths = client.get("/openapi.json").json()["paths"]

    assert "202" in paths["/v1/tasks/export"]["post"]["responses"]
    assert "get" in paths["/v1/jobs/{job_id}"]


def test_existing_routes_still_work():
    response = client.get("/v1/tasks/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["success"] is False


# ---------- N3: unknown and bad job ids ----------


def test_bad_job_id_is_422(monkeypatch):
    fake_async_result(monkeypatch, "PENDING")

    for bad in ["abc", '"69fcb823-30ac-4847-8bdb-2a9c5a4be3e6"', "job-123"]:
        response = client.get(f"/v1/jobs/{bad}")

        assert response.status_code in (404, 422)
        if response.status_code == 422:
            assert response.json()["success"] is False


def test_quoted_job_id_is_rejected_not_reported_as_pending(monkeypatch):
    fake_async_result(monkeypatch, "PENDING")

    response = client.get("/v1/jobs/%2269fcb823-30ac-4847-8bdb-2a9c5a4be3e6%22")

    assert response.status_code == 422


def test_unknown_job_id_is_404(monkeypatch):
    fake_async_result(monkeypatch, "PENDING")
    monkeypatch.setattr("app.routers.jobs.job_exists", lambda job_id: False)

    response = client.get(f"/v1/jobs/{JOB_ID}")

    assert response.status_code == 404
    assert response.json()["message"] == "Job not found"


def test_redis_down_while_checking_a_job_is_503(monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    fake_async_result(monkeypatch, "PENDING")

    def down(job_id):
        raise RedisConnectionError("down")

    monkeypatch.setattr("app.routers.jobs.job_exists", down)

    assert client.get(f"/v1/jobs/{JOB_ID}").status_code == 503


def test_export_remembers_the_job_id_it_queues(monkeypatch):
    remembered = []
    job = FakeJob()
    monkeypatch.setattr("app.routers.tasks.export_tasks_csv", job)
    fake_registry(monkeypatch)
    monkeypatch.setattr("app.routers.tasks.remember_job", remembered.append)

    response = client.post("/v1/tasks/export")

    assert remembered == job.calls == [response.json()["data"]["job_id"]]


def test_the_id_is_written_down_before_the_job_is_queued(monkeypatch):
    order = []

    class Job(FakeJob):
        def apply_async(self, *args, **kwargs):
            order.append("queued")
            return super().apply_async(*args, **kwargs)

    monkeypatch.setattr("app.routers.tasks.export_tasks_csv", Job())
    fake_registry(monkeypatch)
    monkeypatch.setattr(
        "app.routers.tasks.remember_job", lambda j: order.append("remembered")
    )

    client.post("/v1/tasks/export")

    assert order == ["remembered", "queued"]


def test_export_with_redis_down_while_remembering_is_503_and_queues_nothing(
    monkeypatch,
):
    from redis.exceptions import ConnectionError as RedisConnectionError

    def down(job_id):
        raise RedisConnectionError("down")

    job = FakeJob()
    monkeypatch.setattr("app.routers.tasks.export_tasks_csv", job)
    fake_registry(monkeypatch)
    monkeypatch.setattr("app.routers.tasks.remember_job", down)

    assert client.post("/v1/tasks/export").status_code == 503
    assert job.calls == []  # No job runs that the client was told failed.


def test_a_failed_queue_gives_the_lock_and_the_id_back(monkeypatch):
    undone = []
    monkeypatch.setattr(
        "app.routers.tasks.export_tasks_csv", FakeJob(error=OperationalError("down"))
    )
    fake_registry(monkeypatch)
    monkeypatch.setattr(
        "app.routers.tasks.forget_job", lambda j: undone.append("forget")
    )
    monkeypatch.setattr(
        "app.routers.tasks.release_export", lambda j: undone.append("release")
    )

    assert client.post("/v1/tasks/export").status_code == 503

    assert undone == ["release", "forget"]


def test_a_second_export_gets_the_id_of_the_running_one(monkeypatch):
    job = FakeJob()
    monkeypatch.setattr("app.routers.tasks.export_tasks_csv", job)
    fake_registry(monkeypatch)
    monkeypatch.setattr("app.routers.tasks.claim_export", lambda j: "running-job")

    response = client.post("/v1/tasks/export")

    assert response.status_code == 202
    assert response.json()["data"]["job_id"] == "running-job"
    assert response.json()["message"] == "Export already running"
    assert job.calls == []  # Nothing new was queued.


# ---------- N4: download ----------


def test_download_returns_the_csv_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.jobs.settings.EXPORT_DIR", str(tmp_path))
    (tmp_path / f"tasks-{JOB_ID}.csv").write_text("id,title\n1,a\n")

    response = client.get(f"/v1/jobs/{JOB_ID}/download")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "tasks-" in response.headers["content-disposition"]
    assert response.text == "id,title\n1,a\n"


def test_download_of_a_missing_file_is_404(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.jobs.settings.EXPORT_DIR", str(tmp_path))

    assert client.get(f"/v1/jobs/{JOB_ID}/download").status_code == 404


def test_download_cannot_be_used_to_read_other_files(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.jobs.settings.EXPORT_DIR", str(tmp_path))

    response = client.get("/v1/jobs/..%2F..%2Fetc%2Fpasswd/download")

    assert response.status_code in (404, 422)


# ---------- cache helpers ----------


def test_remember_and_find_a_job(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.data = {}

        def set(self, key, value, ex=None):
            self.data[key] = ex

        def exists(self, key):
            return int(key in self.data)

    fake = FakeRedis()
    monkeypatch.setattr(job_registry, "get_redis_client", lambda: fake)

    job_registry.remember_job(JOB_ID)

    assert job_registry.job_exists(JOB_ID) is True
    assert job_registry.job_exists("other") is False
    assert (
        fake.data[job_registry.JOB_PREFIX + JOB_ID]
        == job_registry.settings.JOB_RESULT_TTL_SECONDS
    )


def test_result_store_down_gives_503_not_500(monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    class BrokenResult:
        def __init__(self, job_id, app=None):
            pass

        @property
        def status(self):
            raise RedisConnectionError("result store down")

    monkeypatch.setattr("app.routers.jobs.AsyncResult", BrokenResult)
    monkeypatch.setattr("app.routers.jobs.job_exists", lambda job_id: True)

    response = client.get(f"/v1/jobs/{JOB_ID}")

    assert response.status_code == 503
    assert response.json()["errors"] == ["Job status unavailable"]
