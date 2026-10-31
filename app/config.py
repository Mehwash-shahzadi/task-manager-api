from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DATABASE_URL: str
    REDIS_URL: str
    CACHE_TTL:int
    EVENT_STREAM: str = "events:tasks"
    EVENT_STREAM_MAXLEN: int = 10000
    REDIS_TIMEOUT: float = 1.0
    OUTBOX_POLL_SECONDS: float = 2.0
    OUTBOX_BATCH_SIZE: int = 50
    OUTBOX_MAX_BAD_ATTEMPTS: int = 5
    OUTBOX_RETENTION_DAYS: int = 7
    # Optional: agar ye khali h to tests DATABASE_URL use karte hain, bas database ka naam "tasks_test" rakh kar
    TEST_DATABASE_URL: str = ""



    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
        )

settings = Settings()