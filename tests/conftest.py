import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, create_engine

import app.models  # noqa: F401  tables ko SQLModel.metadata par register karta h
from app.config import settings


def _test_url():
    if settings.TEST_DATABASE_URL:
        return make_url(settings.TEST_DATABASE_URL)
    return make_url(settings.DATABASE_URL).set(database="tasks_test")


# tests rows delete karte hain, is liye ye development database par kabhi nahi chalne chahiye
@pytest.fixture(scope="session")
def test_engine():
    url = _test_url()
    if not (url.database or "").endswith("_test"):
        pytest.exit(f"Refusing to run: database '{url.database}' does not end in '_test'")

    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)
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


class FakeRedis:
    """Stands in for redis_client. Raises `error` (if set) instead of accepting events."""

    def __init__(self):
        self.error = None
        self.calls = []

    def xadd(self, stream, fields, **kwargs):
        self.calls.append((stream, fields))
        if self.error is not None:
            raise self.error


@pytest.fixture
def fake_redis(monkeypatch, test_engine):
    fake = FakeRedis()
    monkeypatch.setattr("app.outbox.redis_client", fake)
    monkeypatch.setattr("app.outbox.engine", test_engine)
    return fake
