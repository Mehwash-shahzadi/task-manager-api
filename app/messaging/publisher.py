"""Writing an event: the task and its event are saved in one database transaction."""

import json
from typing import Any
from uuid import uuid4

from sqlmodel import Session

from app.enums import EventType
from app.models.base import current_time
from app.models.outbox import OutboxEvent
from app.schemas.event import TaskEvent

EVENT_SOURCE = "task-manager-api"


def add_to_outbox(
    session: Session, event_type: EventType, data: dict[str, Any]
) -> None:
    row_id = uuid4()  # The event id is the outbox row id, so one id finds both.
    event = TaskEvent(
        event_id=str(row_id),
        event_type=event_type,
        occurred_at=current_time(),
        source=EVENT_SOURCE,
        data=data,
    )
    # Fail now (TypeError) if the data is not JSON-safe, not later at commit time.
    try:
        payload = event.model_dump(mode="json")
    except ValueError as exc:
        raise TypeError(f"Event data is not JSON-safe: {exc}") from exc
    json.dumps(payload)
    session.add(
        OutboxEvent(id=row_id, event_type=event.event_type.value, payload=payload)
    )
