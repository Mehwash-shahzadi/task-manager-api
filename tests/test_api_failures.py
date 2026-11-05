"""L29: what the task API does when Redis is down, for unknown ids, and under parallel requests."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlmodel import Session, func, select

from app.database import get_session
from app.main import app
from app.models.outbox import OutboxEvent
from app.models.task import Task
from app.redis_client import get_cache_client, get_redis_client


def down(*args, **kwargs):
    raise RedisError("Redis is down")


@pytest.fixture
def client_redis_down(session, monkeypatch):
    """The real cache code runs, but every Redis call it makes fails."""
    for client in (get_cache_client(), get_redis_client()):
        for name in ("get", "set", "delete", "ping", "exists"):
            monkeypatch.setattr(client, name, down)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client_no_cache(test_engine, monkeypatch):
    """Every request gets its own database session, like a real server with threads."""

    def own_session():
        with Session(test_engine) as s:
            yield s

    monkeypatch.setattr("app.cache.cache_invalidate", lambda *keys: None)
    app.dependency_overrides[get_session] = own_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def add_task(session, title="a task") -> Task:
    task = Task(title=title)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


# ---------- Redis down: the API falls back to the database ----------


def test_reads_work_when_redis_is_down(client_redis_down, session):
    task = add_task(session)

    listing = client_redis_down.get("/v1/tasks")
    one = client_redis_down.get(f"/v1/tasks/{task.id}")

    assert listing.status_code == 200
    assert [t["id"] for t in listing.json()["data"]] == [str(task.id)]
    assert one.status_code == 200
    assert one.json()["data"]["title"] == "a task"


def test_writes_work_when_redis_is_down(client_redis_down, session):
    created = client_redis_down.post("/v1/tasks", json={"title": "new"})
    task_id = created.json()["data"]["id"]
    updated = client_redis_down.patch(f"/v1/tasks/{task_id}", json={"title": "changed"})
    deleted = client_redis_down.delete(f"/v1/tasks/{task_id}")

    assert created.status_code == 201
    assert updated.status_code == 200
    assert updated.json()["data"]["title"] == "changed"
    assert deleted.status_code == 204


def test_create_still_saves_the_outbox_event_when_redis_is_down(
    client_redis_down, session
):
    client_redis_down.post("/v1/tasks", json={"title": "new"})

    session.expire_all()
    assert session.exec(select(func.count()).select_from(OutboxEvent)).one() == 1


def test_export_is_503_when_redis_is_down(client_redis_down, monkeypatch):
    # The queue accepts the job, but remembering the job id needs Redis.
    monkeypatch.setattr(
        "app.routers.tasks.export_tasks_csv.delay", lambda: SimpleNamespace(id="x")
    )

    response = client_redis_down.post("/v1/tasks/export")

    assert response.status_code == 503
    assert response.json()["success"] is False


def test_health_is_503_when_redis_is_down(client_redis_down):
    response = client_redis_down.get("/health")

    assert response.status_code == 503


# ---------- 404 for an id that does not exist ----------


@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("patch", {"title": "x"})],
)
def test_unknown_task_is_404_for_every_method(client_no_cache, method, body):
    response = getattr(client_no_cache, method)(
        f"/v1/tasks/{uuid4()}", **({"json": body} if body else {})
    )

    assert response.status_code == 404
    assert response.json()["success"] is False


def test_deleting_an_unknown_task_is_204(client_no_cache):
    response = client_no_cache.delete(f"/v1/tasks/{uuid4()}")

    assert response.status_code == 204


# ---------- parallel requests ----------


def test_parallel_creates_all_succeed(client_no_cache, session):
    def create(i: int) -> int:
        return client_no_cache.post("/v1/tasks", json={"title": f"t{i}"}).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(create, range(20)))

    session.expire_all()
    assert codes == [201] * 20
    assert session.exec(select(func.count()).select_from(Task)).one() == 20
    # One event per task, written in the same transaction as the task.
    assert session.exec(select(func.count()).select_from(OutboxEvent)).one() == 20


def test_parallel_updates_of_one_task_all_succeed(client_no_cache, session):
    task = add_task(session)

    def update(i: int) -> int:
        return client_no_cache.patch(
            f"/v1/tasks/{task.id}", json={"title": f"v{i}"}
        ).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(update, range(10)))

    session.expire_all()
    assert codes == [200] * 10
    assert session.get(Task, task.id).title in {f"v{i}" for i in range(10)}


def test_delete_races_with_update_without_a_500(client_no_cache, session):
    task = add_task(session)

    def act(i: int) -> int:
        if i % 2:
            return client_no_cache.delete(f"/v1/tasks/{task.id}").status_code
        return client_no_cache.patch(
            f"/v1/tasks/{task.id}", json={"title": "x"}
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(act, range(8)))

    assert 500 not in codes
    assert set(codes) <= {200, 204, 404}  # 404 is the update that came late
