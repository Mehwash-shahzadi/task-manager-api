"""The /health answer."""

from fastapi.testclient import TestClient

from app.main import app


def test_health_shows_the_backlog(test_engine, monkeypatch):
    class UpRedis:
        def ping(self):
            return True

    monkeypatch.setattr("app.main.get_engine", lambda: test_engine)
    monkeypatch.setattr("app.main.get_redis_client", UpRedis)
    monkeypatch.setattr("app.main.get_cache_client", UpRedis)
    monkeypatch.setattr("app.main.stream_backlog", lambda: 3)
    monkeypatch.setattr("app.main.pending_count", lambda: 7)

    body = TestClient(app).get("/health").json()

    assert body["outbox_pending"] == 7
    assert body["stream_backlog"] == 3
