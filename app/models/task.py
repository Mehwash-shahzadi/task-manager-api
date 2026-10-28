from sqlmodel import Field
from .base import BaseTable
from app.enums import TaskStatus

class Task(BaseTable, table=True):
    title: str
    description: str | None = None
    status: TaskStatus = Field(default=TaskStatus.PENDING)# Field se model ke field par extra settings ya constraints lagate hain