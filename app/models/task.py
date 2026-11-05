from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum
from sqlmodel import Field, SQLModel

from app.enums import TaskStatus

from .base import BaseTable

TITLE_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 2000
IDEMPOTENCY_KEY_MAX_LENGTH = 100


class TaskBase(SQLModel):
    """The fields a client sends when it creates a task. Task and TaskCreate both inherit
    them, so a new field with its limits is written here once."""

    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    due_date: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class Task(BaseTable, TaskBase, table=True):
    # The database refuses an empty title even if a script skips the API.
    __table_args__ = (CheckConstraint("length(title) > 0", name="title_not_empty"),)

    # values_callable stores "pending" (the value), not "PENDING" (the Python name).
    status: TaskStatus = Field(
        default=TaskStatus.PENDING,
        sa_type=Enum(
            TaskStatus,
            name="taskstatus",
            values_callable=lambda enum_class: [m.value for m in enum_class],
        ),
        sa_column_kwargs={"server_default": TaskStatus.PENDING.value},
    )
    overdue_notified_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    # The Idempotency-Key header of the request that created this task. UNIQUE, so a
    # retry with the same key cannot create a second row (many NULLs are allowed).
    idempotency_key: str | None = Field(
        default=None, max_length=IDEMPOTENCY_KEY_MAX_LENGTH, unique=True
    )
    # SHA-256 of the request that used the key. A retry must send the same body.
    idempotency_hash: str | None = Field(default=None, max_length=64)
