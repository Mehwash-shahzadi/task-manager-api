from typing import Any
from sqlmodel import SQLModel


class CacheEntry(SQLModel):
    key: str
    ttl: int
    value: Any