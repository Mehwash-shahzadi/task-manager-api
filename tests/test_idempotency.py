"""Idempotent create: the same Idempotency-Key gives the same task, but only for the same body."""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import get_session
from app.main import app
from app.models.outbox import OutboxEvent
from app.models.task import Task


def post(client, key, **body):
    body = {"title": "a", **body}
    return client.post("/v1/tasks", json=body, headers={"Idempotency-Key": key})


def test_the_same_key_and_body_returns_the_first_task(client, session):
    first = post(client, "k1", description="x")
    second = post(client, "k1", description="x")

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["data"]["id"] == first.json()["data"]["id"]
    assert len(session.exec(select(Task)).all()) == 1


def test_the_same_key_with_another_title_gives_409(client):
    post(client, "k1", title="first")

    response = post(client, "k1", title="second")

    assert response.status_code == 409
    assert response.json()["success"] is False


def test_the_same_key_with_another_description_or_status_gives_409(client):
    post(client, "k1", description="x")

    assert post(client, "k1", description="y").status_code == 409
    assert post(client, "k1", description="x", status="done").status_code == 409


def test_the_order_of_json_fields_does_not_matter(client):
    client.post(
        "/v1/tasks",
        json={"title": "a", "description": "d"},
        headers={"Idempotency-Key": "k1"},
    )

    response = client.post(
        "/v1/tasks",
        json={"description": "d", "title": "a"},
        headers={"Idempotency-Key": "k1"},
    )

    assert response.status_code == 200


def test_default_values_count_as_the_same_body(client):
    client.post("/v1/tasks", json={"title": "a"}, headers={"Idempotency-Key": "k1"})

    response = client.post(
        "/v1/tasks",
        json={"title": "a", "status": "pending", "description": None},
        headers={"Idempotency-Key": "k1"},
    )

    assert response.status_code == 200


def test_a_task_made_before_the_hash_existed_is_still_replayed(client, session):
    session.add(Task(title="old", idempotency_key="legacy"))  # No hash stored.
    session.commit()

    response = post(client, "legacy", title="whatever")

    assert response.status_code == 200
    assert response.json()["data"]["title"] == "old"


def test_the_hash_is_stored_but_never_shown(client, session):
    response = post(client, "k1")

    assert "idempotency_hash" not in response.json()["data"]
    row = session.exec(select(Task)).one()
    assert len(row.idempotency_hash) == 64


def test_no_key_stores_no_hash(client, session):
    client.post("/v1/tasks", json={"title": "a"})

    assert session.exec(select(Task)).one().idempotency_hash is None


def test_the_409_is_documented(client):
    schema = client.get("/openapi.json").json()

    assert "409" in schema["paths"]["/v1/tasks"]["post"]["responses"]


def count(session, model):
    session.expire_all()
    return len(session.exec(select(model)).all())


def create(client, key=None, title="a"):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/v1/tasks", json={"title": title}, headers=headers)


# ---------- L8: Idempotency-Key ----------


def test_same_key_twice_makes_one_task(client, session):
    first = create(client, "abc")
    second = create(client, "abc")

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["data"]["id"] == first.json()["data"]["id"]
    assert second.headers["location"] == first.headers["location"]
    assert count(session, Task) == 1


def test_the_retry_writes_no_second_outbox_event(client, session):
    create(client, "abc")
    create(client, "abc")

    assert count(session, OutboxEvent) == 1


def test_different_keys_make_different_tasks(client, session):
    create(client, "one")
    create(client, "two")

    assert count(session, Task) == 2


def test_no_key_keeps_the_old_behaviour(client, session):
    first = create(client)
    second = create(client)

    assert first.status_code == second.status_code == 201
    assert count(session, Task) == 2


def test_a_retry_with_a_different_body_is_refused(client, session):
    create(client, "abc", title="first")
    second = create(client, "abc", title="second")

    assert second.status_code == 409
    assert "different request body" in second.json()["message"]
    assert count(session, Task) == 1


@pytest.mark.parametrize("key", ["", "x" * 101])
def test_bad_key_gives_422(client, key):
    assert create(client, key).status_code == 422


def test_the_key_is_not_shown_in_the_answer(client):
    data = create(client, "abc").json()["data"]

    assert "idempotency_key" not in data


def test_the_database_refuses_a_repeated_key(session):
    session.add(Task(title="a", idempotency_key="k"))
    session.commit()
    session.add(Task(title="b", idempotency_key="k"))

    with pytest.raises(Exception, match="uq_task_idempotency_key"):
        session.commit()
    session.rollback()


def test_many_tasks_without_a_key_are_allowed(session):
    session.add(Task(title="a"))
    session.add(Task(title="b"))
    session.commit()  # Two NULLs do not clash.


def test_parallel_requests_with_the_same_key_make_one_task(test_engine):
    """Every request gets its own session, like a real server with threads."""

    def own_session():
        with Session(test_engine) as s:
            yield s

    app.dependency_overrides[get_session] = own_session
    client = TestClient(app)
    barrier = threading.Barrier(6)
    results = []

    def send():
        barrier.wait()
        results.append(create(client, "race"))

    threads = [threading.Thread(target=send) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(r.status_code for r in results)
    ids = {r.json()["data"]["id"] for r in results}
    assert statuses == [200] * 5 + [201]
    assert len(ids) == 1
    with Session(test_engine) as s:
        assert count(s, Task) == 1
        assert count(s, OutboxEvent) == 1
