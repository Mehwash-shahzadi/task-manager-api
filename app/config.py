from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    # The database login is written once, here. DATABASE_URL is built from these parts.
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_HOST: str  # "localhost" when the app runs outside Docker, "db" inside it.
    POSTGRES_PORT: int
    POSTGRES_DB: str
    REDIS_URL: str  # Streams, job ids and health. Never evicts data.
    CACHE_REDIS_URL: str  # Only cached tasks. May drop old keys when full.
    LIST_CACHE_TTL: int  # Seconds the cached task list lives.
    TASK_CACHE_TTL: int  # Seconds one cached task lives.
    NOT_FOUND_CACHE_TTL: int  # Seconds an unknown task id is remembered.
    KEY_PREFIX: str  # Starts every Redis key we write (cache, job ids, seen events).
    EVENT_STREAM: str
    EVENT_STREAM_MAXLEN: int
    REDIS_TIMEOUT: float
    REDIS_MAX_CONNECTIONS: int  # Connection limit for each Redis client.
    DEAD_STREAM_MAXLEN: int  # Dead-letter stream length Redis trims to.
    OUTBOX_POLL_SECONDS: float
    OUTBOX_BATCH_SIZE: int
    OUTBOX_MAX_BAD_ATTEMPTS: int
    OUTBOX_RETENTION_DAYS: int
    CONSUMER_GROUP: str
    CONSUMER_NAME: str  # Stable name for this worker; empty = hostname-pid.
    CONSUMER_BLOCK_MS: int  # How long to wait for a new event, in ms.
    CONSUMER_BATCH_SIZE: int  # Events to read at once.
    CONSUMER_SEEN_TTL_SECONDS: int
    CONSUMER_MIN_IDLE_MS: int
    CONSUMER_MAX_DELIVERIES: int
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str
    EXPORT_DIR: str
    EXPORT_RETENTION_HOURS: int  # Older export files are deleted.
    EXPORT_LOCK_SECONDS: (
        int  # Longest an export may hold the "one export at a time" lock.
    )
    JOB_RESULT_TTL_SECONDS: int  # How long Celery keeps a job result.
    MAX_BODY_BYTES: int  # Larger request bodies get 413.
    DOCS_ENABLED: bool  # False hides /docs and /openapi.json.
    API_KEY: str  # Clients send it in the X-API-Key header. Empty = nobody gets in.

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def DATABASE_URL(self) -> str:
        """The full address, with the password escaped where needed."""
        return URL.create(
            "postgresql+psycopg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            database=self.POSTGRES_DB,
        ).render_as_string(hide_password=False)


settings = Settings()  # type: ignore[call-arg]  # Values are loaded from environment
