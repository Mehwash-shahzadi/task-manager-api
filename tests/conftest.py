import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine

from app.cache import get_task_cache
from app.config import settings
from app.main import app
from app.security import require_api_key
from tests.fakes import FakeCache, FakeRedis, FakeStreamRedis

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def alembic(url, *args, check=True):
    """Run Alembic against the given database. env.py reads the POSTGRES_* settings from the environment."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env={
            **os.environ,
            "POSTGRES_USER": url.username or "",
            "POSTGRES_PASSWORD": url.password or "",
            "POSTGRES_HOST": url.host or "",
            "POSTGRES_PORT": str(url.port),
            "POSTGRES_DB": url.database or "",
        },
        capture_output=True,
        text=True,
        check=check,
    )


def _test_url():
    if os.environ.get("TEST_DATABASE_URL"):
        return make_url(os.environ["TEST_DATABASE_URL"])
    return make_url(settings.DATABASE_URL).set(database="tasks_test")


# Tests delete rows, so they must never run against the development database.
@pytest.fixture(scope="session")
def test_engine():
    url = _test_url()
    if not (url.database or "").endswith("_test"):
        pytest.exit(
            f"Refusing to run: database '{url.database}' does not end in '_test'"
        )

    engine = create_engine(url)
    # Build the tables with the real migrations, so a broken migration fails the tests.
    # The name was checked above, so wiping the schema is safe.
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    alembic(url, "upgrade", "head")
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(test_engine):
    yield
    with Session(test_engine) as session:
        session.execute(text("TRUNCATE outbox_event, task"))
        session.commit()


@pytest.fixture
def session(test_engine):
    with Session(test_engine) as session:
        yield session


@pytest.fixture
def fake_redis(monkeypatch, test_engine):
    fake = FakeRedis()
    monkeypatch.setattr("app.messaging.outbox.get_redis_client", lambda: fake)
    monkeypatch.setattr("app.messaging.outbox.get_engine", lambda: test_engine)
    return fake


@pytest.fixture
def stream_redis(monkeypatch):
    fake = FakeStreamRedis()
    monkeypatch.setattr("app.messaging.consumer.get_consumer_redis", lambda: fake)
    return fake


@pytest.fixture
def fake_cache():
    return FakeCache()


@pytest.fixture(autouse=True)
def route_dependencies(fake_cache):
    """Every test gets a fake cache and no API key check, unless it asks for the real ones.

    Tests that check the real behaviour remove one override:
    app.dependency_overrides.pop(require_api_key) or .pop(get_task_cache).
    """
    app.dependency_overrides[get_task_cache] = lambda: fake_cache
    app.dependency_overrides[require_api_key] = lambda: None
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client(session):
    """The real app, using the test database session."""
    from fastapi.testclient import TestClient

    from app.database import get_session

    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)
