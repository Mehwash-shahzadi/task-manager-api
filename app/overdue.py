import logging

from sqlmodel import Session, col, select

from app.cache import invalidate_tasks
from app.database import get_engine
from app.enums import TaskStatus
from app.models.base import current_time
from app.models.task import Task

logger = logging.getLogger(__name__)
BATCH_SIZE = 100


def mark_overdue() -> int:
    now = current_time()
    ids = []

    with Session(get_engine()) as session:
        rows = session.exec(
            select(Task)
            .where(col(Task.due_date) < now)
            .where(Task.status != TaskStatus.DONE)
            .where(col(Task.overdue_notified_at).is_(None))
            .order_by(col(Task.due_date))
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            row.overdue_notified_at = now
            session.add(row)
            ids.append(row.id)

            logger.info(
                "Task overdue: %s (%s) was due %s",
                row.id,
                row.title,
                row.due_date,
            )
        session.commit()

    # Clear the cache after the commit, or a request could cache the old row again.
    if ids:
        invalidate_tasks(*ids)

    return len(ids)
