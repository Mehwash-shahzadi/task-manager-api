from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DATABASE_URL: str
    REDIS_URL: str
    CACHE_TTL:int


    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
        )

setting=Settings()