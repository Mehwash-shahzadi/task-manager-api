"""One error format: messages, headers, database errors and crashes."""

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from app.database import get_session
from app.exceptions import database_exception_handler, http_exception_handler
from app.main import app


@pytest.fixture
def client(session, monkeypatch):
    def use_test_session():
        yield session

    app.dependency_overrides[get_session] = use_test_session
    monkeypatch.setattr("app.cache.cache_invalidate", lambda *keys: None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def call_handler(exc):
    response = asyncio.run(database_exception_handler(fake_request(), exc))
    return response.status_code, json.loads(response.body)


def fake_request():
    return Request({"type": "http", "method": "GET", "path": "/x", "headers": []})


# ---------- L7: error message ----------


def test_http_error_with_a_dict_detail_gets_a_plain_message():
    exc = HTTPException(status_code=400, detail={"a": 1})

    response = asyncio.run(http_exception_handler(None, exc))

    assert b"Request failed" in response.body
    assert response.status_code == 400


# ---------- L1: error headers are kept ----------


def test_error_headers_are_kept():
    exc = HTTPException(401, detail="no", headers={"WWW-Authenticate": "Bearer"})

    response = asyncio.run(http_exception_handler(None, exc))

    assert response.headers["www-authenticate"] == "Bearer"
    assert response.status_code == 401


def test_error_without_headers_still_works():
    response = asyncio.run(http_exception_handler(None, HTTPException(404, "x")))

    assert response.status_code == 404


# ---------- L6: database errors ----------


def test_database_handler_statuses():
    assert call_handler(OperationalError("s", {}, Exception("x")))[0] == 503
    assert call_handler(IntegrityError("s", {}, Exception("x")))[0] == 409
    assert call_handler(SQLAlchemyError("x"))[0] == 500


def test_database_handler_does_not_leak_details():
    _, body = call_handler(IntegrityError("SELECT secret", {}, Exception("pw=abc")))

    assert "secret" not in json.dumps(body)
    assert "abc" not in json.dumps(body)


def test_database_handler_is_registered():
    assert SQLAlchemyError in app.exception_handlers


# ---------- L2: a crash still gets request id and the error format ----------


def test_unhandled_error_gets_request_id_and_envelope():
    async def boom():
        raise RuntimeError("secret detail")

    app.add_api_route("/__boom", boom)
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/__boom", headers={"X-Request-ID": "abc123"}
        )
    finally:
        app.router.routes.pop()

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "abc123"
    assert "X-Process-Time" in response.headers
    assert response.json()["success"] is False
    assert "secret detail" not in response.text


# ---------- M22: /docs shows the real error answer ----------


def test_docs_describe_the_error_answer(client):
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]

    def ref(path, method, code):
        return paths[path][method]["responses"][code]["content"]["application/json"][
            "schema"
        ]["$ref"]

    assert ref("/v1/tasks", "get", "401").endswith("/ErrorResponse")
    assert ref("/v1/tasks", "get", "422").endswith("/ErrorResponse")
    assert ref("/v1/tasks", "get", "500").endswith("/ErrorResponse")
    assert ref("/v1/tasks/{task_id}", "get", "404").endswith("/ErrorResponse")
    assert ref("/v1/tasks/{task_id}", "patch", "404").endswith("/ErrorResponse")
    assert ref("/v1/tasks/export", "post", "503").endswith("/ErrorResponse")
    assert ref("/v1/jobs/{job_id}", "get", "404").endswith("/ErrorResponse")
    assert "ErrorResponse" in schema["components"]["schemas"]


def test_a_real_404_matches_the_documented_error_model(client):
    from app.schemas.response import ErrorResponse

    response = client.get(f"/v1/tasks/{uuid4()}")

    body = ErrorResponse.model_validate(response.json())
    assert body.success is False
    assert body.errors
