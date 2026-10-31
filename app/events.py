import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlmodel import Session

from app.models.outbox import OutboxEvent

EVENT_SOURCE = "task-manager-api"

def add_to_outbox(session: Session, event_type: str, data: dict[str, Any]) -> None:
    event = {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source": EVENT_SOURCE,
        "data": data,
    }
    session.add(OutboxEvent(event_type=event_type, payload=event))
