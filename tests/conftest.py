import pytest
from redis.exceptions import ResponseError
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


class FakeStreamRedis:
    """Stands in for consumer_redis. Keeps streams, consumer groups, pending entries and keys in memory."""

    def __init__(self):
        self.streams = {}  # stream name -> list of (id, fields)
        self.groups = {}  # (stream, group) -> {"last": index of last delivered entry, "pending": {id: info}}
        self.keys = {}  # key -> (value, ttl)
        self.now_ms = 0
        self._counter = 0

    def advance(self, ms):
        """Move the fake clock forward, so pending entries become idle."""
        self.now_ms += ms

    def entries(self, stream):
        return list(self.streams.get(stream, []))

    def pending_ids(self, stream, group):
        return list(self.groups[(stream, group)]["pending"])

    def xgroup_create(self, name, groupname, id="$", mkstream=False):
        if name not in self.streams:
            if not mkstream:
                raise ResponseError("ERR The XGROUP subcommand requires the key to exist")
            self.streams[name] = []
        if (name, groupname) in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        last = -1 if id == "0" else len(self.streams[name]) - 1
        self.groups[(name, groupname)] = {"last": last, "pending": {}}

    def xadd(self, name, fields, **kwargs):
        self._counter += 1
        entry_id = f"{self._counter}-0"
        self.streams.setdefault(name, []).append((entry_id, dict(fields)))
        return entry_id

    def xreadgroup(self, groupname, consumername, streams, count=None, block=None):
        result = []
        for name in streams:
            group = self.groups[(name, groupname)]
            new = self.streams[name][group["last"] + 1:]
            if count is not None:
                new = new[:count]
            if not new:
                continue
            group["last"] += len(new)
            for entry_id, _ in new:
                group["pending"][entry_id] = {
                    "consumer": consumername,
                    "delivered_at": self.now_ms,
                    "times": 1,
                }
            result.append([name, [(entry_id, dict(fields)) for entry_id, fields in new]])
        return result

    def xack(self, name, groupname, *ids):
        pending = self.groups[(name, groupname)]["pending"]
        return sum(1 for entry_id in ids if pending.pop(entry_id, None) is not None)

    def xpending_range(self, name, groupname, min, max, count):
        pending = self.groups[(name, groupname)]["pending"]
        return [
            {
                "message_id": entry_id,
                "consumer": info["consumer"],
                "time_since_delivered": self.now_ms - info["delivered_at"],
                "times_delivered": info["times"],
            }
            for entry_id, info in pending.items()
            if min <= entry_id <= max
        ][:count]

    def xautoclaim(self, name, groupname, consumername, min_idle_time, start_id="0-0", count=None):
        pending = self.groups[(name, groupname)]["pending"]
        claimed = []
        for entry_id, info in pending.items():
            if count is not None and len(claimed) >= count:
                break
            if self.now_ms - info["delivered_at"] >= min_idle_time:
                info["consumer"] = consumername
                info["delivered_at"] = self.now_ms
                info["times"] += 1
                fields = dict(next(f for i, f in self.streams[name] if i == entry_id))
                claimed.append((entry_id, fields))
        return ("0-0", claimed, [])

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = (value, ex)
        return True

    def exists(self, key):
        return int(key in self.keys)

    def ttl(self, key):
        return self.keys[key][1]


@pytest.fixture
def stream_redis(monkeypatch):
    fake = FakeStreamRedis()
    monkeypatch.setattr("app.consumer.consumer_redis", fake)
    return fake
