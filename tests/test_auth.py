"""The X-API-Key check on every route."""

from uuid import uuid4

import pytest

from app.config import settings
from app.main import app
from app.security import require_api_key


@pytest.fixture
def real_auth(monkeypatch):
    """Turn the key check back on (the other tests switch it off) and set a known key."""
    app.dependency_overrides.pop(require_api_key)
    monkeypatch.setattr(settings, "API_KEY", "secret-key")


PROTECTED = [
    ("get", "/v1/tasks"),
    ("post", "/v1/tasks"),
    ("get", f"/v1/tasks/{uuid4()}"),
    ("patch", f"/v1/tasks/{uuid4()}"),
    ("delete", f"/v1/tasks/{uuid4()}"),
    ("post", "/v1/tasks/export"),
    ("get", f"/v1/jobs/{uuid4()}"),
    ("get", "/v1/cache"),
]


# ---------- B15: X-API-Key ----------


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_no_key_gives_401(real_auth, client, method, path):
    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert response.json()["success"] is False  # Our usual error envelope.
    assert response.headers["www-authenticate"] == "ApiKey"


@pytest.mark.parametrize("key", ["wrong", "secret-ke", "secret-key2", "SECRET-KEY"])
def test_wrong_key_gives_401(real_auth, client, key):
    response = client.get("/v1/tasks", headers={"X-API-Key": key})

    assert response.status_code == 401


def test_non_ascii_key_gives_401_not_500(real_auth, client):
    response = client.get("/v1/tasks", headers={"X-API-Key": "é".encode("latin-1")})

    assert response.status_code == 401


def test_right_key_gives_200(real_auth, client):
    response = client.get("/v1/tasks", headers={"X-API-Key": "secret-key"})

    assert response.status_code == 200


def test_empty_api_key_setting_lets_nobody_in(real_auth, client, monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "")

    assert client.get("/v1/tasks", headers={"X-API-Key": ""}).status_code == 401
    assert client.get("/v1/tasks").status_code == 401


def test_health_needs_no_key(real_auth, client):
    # 200 when Redis and the database are up, 503 when not. Never 401.
    assert client.get("/health").status_code != 401


def test_docs_describe_the_key_header(client):
    schema = client.get("/openapi.json").json()

    assert "APIKeyHeader" in schema["components"]["securitySchemes"]
