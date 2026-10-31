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
    CONSUMER_GROUP	: str="task-workers"
    CONSUMER_BLOCK_MS: int=5000#Naye event ke liye kitni der wait karna hai?
    CONSUMER_BATCH_SIZE: int=10#Ek read mein kitne events maangne hain
    CONSUMER_SEEN_TTL_SECONDS: int=86400
    CONSUMER_MIN_IDLE_MS: int = 60000
    CONSUMER_MAX_DELIVERIES: int = 3



    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
        )

settings = Settings()