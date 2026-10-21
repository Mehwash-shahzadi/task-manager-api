from datetime import datetime
from uuid import UUID

from sqlmodel import SQLModel
from app.enums import TaskStatus


class TaskCreate(SQLModel):
    title: str
    description: str | None = None
    status: TaskStatus = TaskStatus.PENDING


class TaskUpdate(SQLModel):
    title: str | None = None
    description: str | None = None
    status: TaskStatus | None = None


class TaskRead(SQLModel):
    id: UUID
    title: str
    description: str | None
    status: TaskStatus
    created_at: datetime
    updated_at: datetime