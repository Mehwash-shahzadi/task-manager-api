"""Task 10: due_date and overdue_notified_at on tasks. Calls the real routes with the test database."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import select

from app.cache import LIST_KEY, task_key
from app.database import get_session
from app.main import app, create_app
from app.models.outbox import OutboxEvent
from app.models.task import Task
from app.schemas.task import TaskUpdate

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DUE = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
MARKED = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)


@pytest.fixture
def client(session, monkeypatch):
    """Routes use the test database and never touch the real Redis cache."""

    def use_test_session():
        yield session

    app.dependency_overrides[get_session] = use_test_session
    monkeypatch.setattr("app.cache.cache_invalidate", lambda *keys: None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def add_task(session, title="a task", **fields) -> Task:
    task = Task(title=title, **fields)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def reload(session, task) -> Task:
    session.expire_all()
    return session.get(Task, task.id)


# ---------- model ----------


@pytest.mark.parametrize("column", ["due_date", "overdue_notified_at"])
def test_columns_are_nullable_and_timezone_aware(column):
    col = Task.__table__.c[column]

    assert col.nullable is True
    assert col.type.timezone is True


# ---------- create ----------


def test_create_with_due_date(client, session):
    response = client.post(
        "/v1/tasks", json={"title": "deadline", "due_date": "2026-11-05T10:00:00Z"}
    )

    assert response.status_code == 201
    data = response.json()["data"]
    assert datetime.fromisoformat(data["due_date"]) == DUE
    assert data["overdue_notified_at"] is None
    row = session.exec(select(Task)).one()
    assert row.due_date == DUE


def test_create_without_due_date_gives_null(client, session):
    data = client.post("/v1/tasks", json={"title": "no deadline"}).json()["data"]

    assert data["due_date"] is None
    assert data["overdue_notified_at"] is None
    assert session.exec(select(Task)).one().due_date is None


def test_client_cannot_set_overdue_notified_at_on_create(client, session):
    body = {"title": "sneaky", "overdue_notified_at": "2026-01-01T00:00:00Z"}

    response = client.post("/v1/tasks", json=body)

    assert response.status_code == 422
    assert session.exec(select(Task)).all() == []


def test_create_event_carries_the_due_date(client, session):
    client.post(
        "/v1/tasks", json={"title": "deadline", "due_date": "2026-11-05T10:00:00Z"}
    )

    event = session.exec(select(OutboxEvent)).one()
    assert datetime.fromisoformat(event.payload["data"]["due_date"]) == DUE


# ---------- read ----------


def test_get_returns_both_new_fields(client, session):
    task = add_task(session, due_date=DUE, overdue_notified_at=MARKED)

    data = client.get(f"/v1/tasks/{task.id}").json()["data"]

    assert datetime.fromisoformat(data["due_date"]) == DUE
    assert datetime.fromisoformat(data["overdue_notified_at"]) == MARKED


def test_list_returns_both_new_fields(client, session):
    add_task(session, due_date=DUE)

    row = client.get("/v1/tasks").json()["data"][0]

    assert "due_date" in row
    assert "overdue_notified_at" in row


def test_old_cached_copy_without_the_new_keys_still_reads(client, fake_cache):
    # a task cached before this change has no due_date / overdue_notified_at keys
    old_copy = {
        "id": str(uuid4()),
        "title": "cached before deploy",
        "description": None,
        "status": "pending",
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-01T00:00:00Z",
    }
    fake_cache.store[task_key(old_copy["id"])] = old_copy

    response = client.get(f"/v1/tasks/{old_copy['id']}")

    assert response.status_code == 200
    assert response.json()["data"]["due_date"] is None


# ---------- update ----------


def test_put_can_set_a_due_date(client, session):
    task = add_task(session)

    response = client.patch(
        f"/v1/tasks/{task.id}", json={"due_date": "2026-11-05T10:00:00Z"}
    )

    assert response.status_code == 200
    assert reload(session, task).due_date == DUE


def test_client_cannot_set_overdue_notified_at_on_update(client, session):
    task = add_task(session, due_date=DUE)

    response = client.patch(
        f"/v1/tasks/{task.id}", json={"overdue_notified_at": "2026-01-01T00:00:00Z"}
    )

    assert response.status_code == 422
    assert reload(session, task).overdue_notified_at is None


def test_changing_the_due_date_clears_overdue_notified_at(client, session):
    task = add_task(session, due_date=DUE, overdue_notified_at=MARKED)

    response = client.patch(
        f"/v1/tasks/{task.id}", json={"due_date": "2026-11-20T10:00:00Z"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["overdue_notified_at"] is None
    assert reload(session, task).overdue_notified_at is None


def test_same_due_date_keeps_overdue_notified_at(client, session):
    task = add_task(session, due_date=DUE, overdue_notified_at=MARKED)

    client.patch(f"/v1/tasks/{task.id}", json={"due_date": "2026-11-05T10:00:00Z"})

    assert reload(session, task).overdue_notified_at == MARKED


def test_same_moment_in_another_time_zone_keeps_overdue_notified_at(client, session):
    # 15:00 at +05:00 is 10:00 UTC: the same moment, so nothing changed
    task = add_task(session, due_date=DUE, overdue_notified_at=MARKED)

    client.patch(f"/v1/tasks/{task.id}", json={"due_date": "2026-11-05T15:00:00+05:00"})

    assert reload(session, task).overdue_notified_at == MARKED


def test_changing_only_the_title_keeps_overdue_notified_at(client, session):
    task = add_task(session, due_date=DUE, overdue_notified_at=MARKED)

    client.patch(f"/v1/tasks/{task.id}", json={"title": "new title"})

    row = reload(session, task)
    assert row.title == "new title"
    assert row.overdue_notified_at == MARKED


def test_changing_only_the_status_keeps_overdue_notified_at(client, session):
    task = add_task(session, due_date=DUE, overdue_notified_at=MARKED)

    client.patch(f"/v1/tasks/{task.id}", json={"status": "done"})

    assert reload(session, task).overdue_notified_at == MARKED


def test_setting_due_date_to_null_clears_it_and_the_marker(client, session):
    task = add_task(session, due_date=DUE, overdue_notified_at=MARKED)

    response = client.patch(f"/v1/tasks/{task.id}", json={"due_date": None})

    assert response.status_code == 200
    row = reload(session, task)
    assert row.due_date is None
    assert row.overdue_notified_at is None


def test_update_does_not_change_created_at(client, session):
    task = add_task(session, due_date=DUE)
    created = task.created_at

    client.patch(f"/v1/tasks/{task.id}", json={"due_date": "2026-11-20T10:00:00Z"})

    assert reload(session, task).created_at == created


# ---------- migration ----------


def test_a_migration_adds_both_columns():
    versions = PROJECT_ROOT / "alembic" / "versions"
    matches = [
        p
        for p in versions.glob("*.py")
        if "due_date" in p.read_text() and "overdue_notified_at" in p.read_text()
    ]

    assert len(matches) == 1, (
        "expected one migration that adds due_date and overdue_notified_at"
    )
    text = matches[0].read_text()
    assert text.count("op.add_column") == 2
    assert text.count("op.drop_column") == 2
    assert "timezone=True" in text
    assert "down_revision" in text and "503921b3fe0f" in text


# ---------- input limits: min/max length and unknown fields ----------


@pytest.mark.parametrize("title", ["", "   "])
def test_create_rejects_an_empty_title(client, session, title):
    response = client.post("/v1/tasks", json={"title": title})

    assert response.status_code == 422
    assert session.exec(select(Task)).all() == []


def test_create_rejects_a_title_longer_than_200_characters(client):
    assert client.post("/v1/tasks", json={"title": "x" * 201}).status_code == 422


def test_create_accepts_a_title_of_exactly_200_characters(client):
    assert client.post("/v1/tasks", json={"title": "x" * 200}).status_code == 201


def test_create_rejects_a_description_longer_than_2000_characters(client):
    body = {"title": "ok", "description": "x" * 2001}

    assert client.post("/v1/tasks", json=body).status_code == 422


def test_create_strips_spaces_around_the_title(client):
    data = client.post("/v1/tasks", json={"title": "  hello  "}).json()["data"]

    assert data["title"] == "hello"


def test_create_rejects_an_unknown_field(client):
    assert (
        client.post("/v1/tasks", json={"title": "ok", "colour": "red"}).status_code
        == 422
    )


def test_update_rejects_an_empty_title(client, session):
    task = add_task(session)

    assert client.patch(f"/v1/tasks/{task.id}", json={"title": ""}).status_code == 422
    assert reload(session, task).title == "a task"


def test_update_rejects_a_title_longer_than_200_characters(client, session):
    task = add_task(session)

    assert (
        client.patch(f"/v1/tasks/{task.id}", json={"title": "x" * 201}).status_code
        == 422
    )


def test_update_rejects_an_unknown_field(client, session):
    task = add_task(session)

    assert (
        client.patch(f"/v1/tasks/{task.id}", json={"colour": "red"}).status_code == 422
    )


def test_validation_errors_use_the_error_envelope(client):
    body = client.post("/v1/tasks", json={"title": ""}).json()

    assert body["success"] is False
    assert body["message"] == "Validation error"


def test_not_found_uses_the_error_envelope_and_status(client):
    response = client.get(f"/v1/tasks/{uuid4()}")

    body = response.json()
    assert response.status_code == 404
    assert body["success"] is False
    assert body["message"] == "Task not found"
    assert body["errors"] == ["Task not found"]


def test_validation_error_is_422_not_400(client):
    assert client.post("/v1/tasks", json={"title": ""}).status_code == 422


# ---------- batch 4 ----------


def test_create_returns_a_location_header(client):
    response = client.post("/v1/tasks", json={"title": "new"})

    assert response.status_code == 201
    task_id = response.json()["data"]["id"]
    assert response.headers["Location"] == f"/v1/tasks/{task_id}"


def test_delete_works_and_the_task_is_gone(client, session):
    task = add_task(session)

    assert client.delete(f"/v1/tasks/{task.id}").status_code == 204
    assert client.get(f"/v1/tasks/{task.id}").status_code == 404


def test_update_with_null_title_or_status_is_422(client, session):
    task = add_task(session)

    for field in ("title", "status"):
        response = client.patch(f"/v1/tasks/{task.id}", json={field: None})
        assert response.status_code == 422


def test_event_has_a_version_and_its_id_is_the_outbox_row_id(client, session):
    client.post("/v1/tasks", json={"title": "new"})

    row = session.exec(select(OutboxEvent)).one()
    assert row.payload["version"] == 1
    assert row.payload["event_id"] == str(row.id)


def test_docs_can_be_turned_off(monkeypatch):
    monkeypatch.setattr("app.main.settings.DOCS_ENABLED", False)

    fresh = create_app()

    assert fresh.docs_url is None
    assert fresh.openapi_url is None


def test_create_app_gives_a_new_app_each_time():
    first, second = create_app(), create_app()

    assert first is not second
    for built in (first, second):
        paths = set(built.openapi()["paths"])
        assert {"/health", "/v1/tasks", "/v1/cache"} <= paths


# ---------- due_date needs a time zone ----------


@pytest.mark.parametrize(
    "value", ["2026-11-05T09:00:00", "2026-11-05", "2026-11-05 09:00"]
)
def test_create_rejects_a_due_date_without_a_time_zone(client, value):
    response = client.post("/v1/tasks", json={"title": "a", "due_date": value})

    assert response.status_code == 422
    assert "due_date" in response.json()["errors"][0]


def test_patch_rejects_a_due_date_without_a_time_zone(client, session):
    task = add_task(session)

    response = client.patch(
        f"/v1/tasks/{task.id}", json={"due_date": "2026-11-05T09:00:00"}
    )

    assert response.status_code == 422


@pytest.mark.parametrize("value", ["2026-11-05T10:00:00Z", "2026-11-05T15:00:00+05:00"])
def test_a_due_date_with_a_time_zone_is_still_accepted(client, value):
    response = client.post("/v1/tasks", json={"title": "a", "due_date": value})

    assert response.status_code == 201
    assert datetime.fromisoformat(response.json()["data"]["due_date"]) == DUE


def stale(*args, **kwargs):
    raise StaleDataError("gone")


def make_tasks(session, n):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n):
        add_task(session, title=f"t{i}", created_at=base + timedelta(minutes=i))


def titles(response):
    return [t["title"] for t in response.json()["data"]]


def count(session, model):
    session.expire_all()
    return len(session.exec(select(model)).all())


def create(client, key=None, title="a"):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/v1/tasks", json={"title": title}, headers=headers)


def test_create_and_update_return_the_same_shape_as_a_read(session, monkeypatch):
    from app.database import get_session

    def use_test_session():
        yield session

    app.dependency_overrides[get_session] = use_test_session
    monkeypatch.setattr("app.cache.cache_invalidate", lambda *keys: None)
    try:
        client = TestClient(app)
        created = client.post("/v1/tasks", json={"title": "a"}).json()["data"]
        read = client.get(f"/v1/tasks/{created['id']}").json()["data"]
        updated = client.patch(
            f"/v1/tasks/{created['id']}", json={"title": "b"}
        ).json()["data"]
    finally:
        app.dependency_overrides.clear()

    assert set(created) == set(read) == set(updated)


# ---------- C8: null title/status rejected by the schema ----------


@pytest.mark.parametrize("field", ["title", "status"])
def test_schema_rejects_explicit_null(field):
    with pytest.raises(ValidationError, match=f"{field} cannot be null"):
        TaskUpdate.model_validate({field: None})


def test_schema_allows_missing_and_null_description():
    assert TaskUpdate.model_validate({}).model_fields_set == set()
    assert TaskUpdate.model_validate({"description": None}).description is None


@pytest.mark.parametrize("field", ["title", "status"])
def test_put_null_is_422(client, session, field):
    task = add_task(session)

    response = client.patch(f"/v1/tasks/{task.id}", json={field: None})

    assert response.status_code == 422
    assert response.json()["success"] is False


# ---------- L3: a task deleted meanwhile gives 404 ----------


def test_update_of_vanished_task_is_404(client, session, monkeypatch):
    task = add_task(session)

    def stale_when_there_is_a_change(*args, **kwargs):
        # The lock query flushes first with nothing to send. Only the real UPDATE fails.
        if session.dirty:
            stale()

    monkeypatch.setattr(session, "flush", stale_when_there_is_a_change)

    response = client.patch(f"/v1/tasks/{task.id}", json={"title": "new"})

    assert response.status_code == 404


def test_delete_of_vanished_task_is_204(client, session, monkeypatch):
    task = add_task(session)
    monkeypatch.setattr(session, "flush", stale)

    response = client.delete(f"/v1/tasks/{task.id}")

    assert response.status_code == 204


# ---------- L9: nothing can fail after the commit ----------


def test_create_and_update_do_not_refresh_after_commit(client, session, monkeypatch):
    task = add_task(session)

    def boom(*args, **kwargs):
        raise AssertionError("refresh must not be called")

    monkeypatch.setattr(session, "refresh", boom)

    created = client.post("/v1/tasks", json={"title": "x"})
    updated = client.patch(f"/v1/tasks/{task.id}", json={"title": "y"})

    assert created.status_code == 201
    assert created.headers["Location"] == f"/v1/tasks/{created.json()['data']['id']}"
    assert updated.status_code == 200
    assert updated.json()["data"]["title"] == "y"


def test_update_changes_updated_at(client, session):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    task = add_task(session, created_at=old, updated_at=old)

    response = client.patch(f"/v1/tasks/{task.id}", json={"title": "new"})

    new = datetime.fromisoformat(response.json()["data"]["updated_at"])
    assert new > old + timedelta(days=1)


def test_create_still_writes_outbox_event(client, session):
    client.post("/v1/tasks", json={"title": "x"})

    session.expire_all()
    assert session.query(OutboxEvent).count() == 1


# ---------- G7: order and limit ----------


def test_list_is_ordered_by_creation(client, session):
    make_tasks(session, 4)

    assert titles(client.get("/v1/tasks")) == ["t0", "t1", "t2", "t3"]


def test_list_limit_and_offset(client, session):
    make_tasks(session, 5)

    assert titles(client.get("/v1/tasks?limit=2")) == ["t0", "t1"]
    assert titles(client.get("/v1/tasks?limit=2&offset=2")) == ["t2", "t3"]
    assert titles(client.get("/v1/tasks?offset=10")) == []


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1"])
def test_list_rejects_bad_paging(client, query):
    assert client.get(f"/v1/tasks?{query}").status_code == 422


def test_only_default_page_is_cached(client, session, fake_cache):
    make_tasks(session, 3)

    client.get("/v1/tasks?limit=2")
    assert fake_cache.store == {}

    client.get("/v1/tasks")
    assert list(fake_cache.store) == [LIST_KEY]


# ---------- L4: DELETE gives the same answer every time ----------


def test_delete_twice_gives_204_both_times(client, session):
    task_id = create(client).json()["data"]["id"]

    first = client.delete(f"/v1/tasks/{task_id}")
    second = client.delete(f"/v1/tasks/{task_id}")

    assert first.status_code == second.status_code == 204
    assert client.get(f"/v1/tasks/{task_id}").status_code == 404


def test_delete_of_an_already_gone_task_still_clears_its_cached_copy(
    client, fake_cache
):
    task_id = uuid4()
    fake_cache.store[task_key(task_id)] = {"stale": True}

    assert client.delete(f"/v1/tasks/{task_id}").status_code == 204

    assert task_key(task_id) not in fake_cache.store


# ---------- M24: routes live under /v1 ----------


def test_routes_live_under_v1_and_the_old_paths_are_gone(client):
    assert client.get("/v1/tasks").status_code == 200
    assert client.get("/tasks").status_code == 404
    assert client.get("/jobs/" + str(uuid4())).status_code == 404
    assert client.get("/cache").status_code == 404


def test_health_stays_outside_v1(client):
    assert client.get("/health").status_code == 200
    assert client.get("/v1/health").status_code == 404


def test_location_header_includes_the_prefix(client):
    response = create(client)

    task_id = response.json()["data"]["id"]
    assert response.headers["location"] == f"/v1/tasks/{task_id}"


# ---------- G6: PATCH changes some fields, PUT is gone ----------


def test_patch_changes_only_the_fields_you_send(client):
    created = create(client, title="keep me").json()["data"]

    response = client.patch(f"/v1/tasks/{created['id']}", json={"status": "done"})

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "done"
    assert response.json()["data"]["title"] == "keep me"


def test_put_is_not_allowed_any_more(client):
    task_id = create(client).json()["data"]["id"]

    response = client.put(f"/v1/tasks/{task_id}", json={"title": "x"})

    assert response.status_code == 405
    assert response.json()["success"] is False
