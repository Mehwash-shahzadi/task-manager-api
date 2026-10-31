from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime
from sqlmodel import Field

from .base import BaseTable, current_time

PENDING = "pending"
SENT = "sent"
DEAD = "dead"


class OutboxEvent(BaseTable, table=True):
    __tablename__ = "outbox_event"

    event_type: str
    payload: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default=PENDING, index=True)
    attempts: int = 0
    bad_attempts: int = 0
    last_error: str | None = None
    next_attempt_at: datetime = Field(
        default_factory=current_time, sa_type=DateTime(timezone=True)
    )
    sent_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
