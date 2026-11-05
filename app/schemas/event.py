from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.enums import EventType


class TaskEvent(BaseModel):
    """The event as it travels through the outbox and the stream.

    The API builds it (add_to_outbox) and the consumer checks it with the same class,
    so both sides agree on the shape.
    """

    # strict: an int like 123 is not accepted as an id.
    event_id: str = Field(min_length=1, strict=True)
    event_type: EventType  # A name we do not know is rejected.
    version: int = 1
    occurred_at: datetime | None = None
    source: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
