from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, model_validator
from sqlmodel import Field, SQLModel

from app.enums import TaskStatus
from app.models.task import DESCRIPTION_MAX_LENGTH, TITLE_MAX_LENGTH, TaskBase


class TaskCreate(TaskBase):
    # title, description and due_date come from TaskBase.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)  # type: ignore[assignment]

    status: TaskStatus = TaskStatus.PENDING
    # A time zone is required: "2026-11-05T09:00:00" alone could mean different moments.
    due_date: AwareDatetime | None = None


class TaskUpdate(SQLModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)  # type: ignore[assignment]

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    status: TaskStatus | None = None
    due_date: AwareDatetime | None = None

    # Neither field can be null in the database, so an explicit null is rejected here.
    @model_validator(mode="after")
    def no_null_title_or_status(self):
        for field in ("title", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class TaskRead(SQLModel):
    id: UUID
    title: str
    description: str | None
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    due_date: datetime | None = None
    overdue_notified_at: datetime | None = None
