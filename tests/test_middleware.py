"""Request id, body size limit and request timing."""

import logging

import pytest
from fastapi.testclient import TestClient

from app.database import get_session
from app.main import app


@pytest.fixture
def client(session, monkeypatch):
    def use_test_session():
        yield session

    app.dependency_overrides[get_session] = use_test_session
    monkeypatch.setattr("app.cache.cache_invalidate", lambda *keys: None)
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------- G13: request id ----------


def test_response_has_a_request_id_header(client):
    response = client.get("/v1/tasks")

    assert response.headers["X-Request-ID"]


def test_request_id_from_the_client_is_kept(client):
    response = client.get("/v1/tasks", headers={"X-Request-ID": "abc"})

    assert response.headers["X-Request-ID"] == "abc"


def test_log_lines_carry_the_request_id(client, caplog):
    import logging

    from app.logging_config import RequestIdFilter

    caplog.handler.addFilter(RequestIdFilter())
    with caplog.at_level(logging.INFO, logger="app.middleware"):
        client.get("/v1/tasks", headers={"X-Request-ID": "abc"})

    assert any(getattr(r, "request_id", None) == "abc" for r in caplog.records)


# ---------- M30: body limit ----------


def test_too_large_body_gets_413_in_the_error_envelope(client, monkeypatch):
    monkeypatch.setattr("app.middleware.settings.MAX_BODY_BYTES", 10)

    response = client.post("/v1/tasks", json={"title": "this is longer than ten bytes"})

    assert response.status_code == 413
    assert response.json()["success"] is False


def test_small_body_still_works(client):
    assert client.post("/v1/tasks", json={"title": "ok"}).status_code == 201


# ---------- E5: one middleware does the timing and the logging ----------


def test_response_has_the_timing_and_request_id_headers(monkeypatch):

    response = TestClient(app).get("/v1/tasks")

    assert float(response.headers["X-Process-Time"]) >= 0
    assert response.headers["X-Request-ID"]


def test_log_line_shows_the_real_time_not_n_a(monkeypatch, caplog):

    with caplog.at_level(logging.INFO, logger="app.middleware"):
        TestClient(app).get("/v1/tasks")

    assert "n/a" not in caplog.text


def test_the_old_timing_middleware_is_gone():
    import app.middleware as middleware

    assert not hasattr(middleware, "add_process_time_header")


def test_413_is_also_timed_by_the_outer_middleware(monkeypatch):
    monkeypatch.setattr("app.middleware.settings.MAX_BODY_BYTES", 5)

    response = TestClient(app).post("/v1/tasks", json={"title": "too long for five"})

    assert response.status_code == 413
    assert "X-Process-Time" in response.headers
