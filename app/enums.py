from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class EventType(StrEnum):
    """Every kind of event we publish. The one place the names are written."""

    TASK_CREATED = "task.created"
