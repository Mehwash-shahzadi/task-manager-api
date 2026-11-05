from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Column, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from .base import BaseTable


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DEAD = "dead"


class OutboxEvent(BaseTable, table=True):
    __tablename__ = "outbox_event"
    __table_args__ = (
        # The relay asks for rows by status and next_attempt_at, so one index covers both.
        # It also serves "all rows with this status", so status needs no index of its own.
        Index("ix_outbox_event_status_next_attempt", "status", "next_attempt_at"),
        # The hourly cleanup asks for old sent rows. Only sent rows are in this index.
        Index(
            "ix_outbox_event_sent_at_sent",
            "sent_at",
            postgresql_where=text("status = 'sent'"),
        ),
    )

    event_type: str
    payload: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    # sa_type=String keeps the column a plain VARCHAR (no Postgres ENUM type).
    status: OutboxStatus = Field(
        default=OutboxStatus.PENDING,
        sa_type=String,
        sa_column_kwargs={"server_default": OutboxStatus.PENDING.value},
    )
    attempts: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    bad_attempts: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    last_error: str | None = None
    # Set by the database (now) on insert, so the first send does not depend on this
    # server's clock. The relay changes it later with the database time too.
    next_attempt_at: datetime = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )
    sent_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
