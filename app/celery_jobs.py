import csv
import logging
import time
from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from app.celery_app import celery_app
from app.config import settings
from app.database import get_engine
from app.job_registry import release_export
from app.messaging.monitor import check_dead, check_stream_backlog
from app.messaging.outbox import cleanup_dead, cleanup_sent, relay_once
from app.models.task import Task
from app.overdue import mark_overdue

logger = logging.getLogger(__name__)


@celery_app.task  # it register the function as a celery task
def ping():
    return "pong"


@celery_app.task(ignore_result=True)
def cleanup_outbox():
    try:
        deleted = cleanup_sent() + cleanup_dead()
        logger.info("Outbox cleanup deleted %s rows", deleted)
        return deleted
    except Exception:
        logger.exception("Outbox cleanup failed")
        raise


@celery_app.task(ignore_result=True)
def watch_stream():
    return check_stream_backlog()


@celery_app.task(ignore_result=True)
def watch_outbox():
    return check_dead()


@celery_app.task(ignore_result=True)
def relay_outbox():
    try:
        sent = relay_once()
        if sent > 0:
            logger.info("Outbox relay sent %s events", sent)
        return sent
    except OperationalError:
        # The relay runs every 2 s, so a long traceback each time would flood the logs.
        logger.warning("Outbox relay skipped: database unavailable")
        raise
    except Exception:
        logger.exception("Outbox relay failed")
        raise


EXPORT_HEADER = ["id", "title", "description", "status", "created_at", "updated_at"]
EXPORT_BATCH_SIZE = 500  # Rows read from the database at a time.


def safe_cell(value: str) -> str:
    """Excel runs a cell that starts with one of these characters as a formula."""
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


@celery_app.task(bind=True)
def export_tasks_csv(self):
    try:
        return write_export(self.request.id)
    finally:
        # Let the next export start, also when this one failed.
        release_export(self.request.id)


def write_export(job_id: str) -> dict:
    folder = Path(settings.EXPORT_DIR)
    folder.mkdir(parents=True, exist_ok=True)

    path = folder / f"tasks-{job_id}.csv"
    # Write to a temporary file first, so a crash never leaves a half file.
    tmp_path = path.with_suffix(".tmp")
    rows = 0

    try:
        with (
            Session(get_engine()) as session,
            open(tmp_path, "w", newline="", encoding="utf-8") as f,
        ):
            writer = csv.writer(f)
            writer.writerow(EXPORT_HEADER)

            # yield_per reads a few rows at a time instead of loading all of them.
            tasks = session.exec(
                select(Task)
                .order_by(col(Task.created_at))
                .execution_options(yield_per=EXPORT_BATCH_SIZE)
            )
            for task in tasks:
                writer.writerow(
                    [
                        str(task.id),
                        safe_cell(task.title),
                        safe_cell(task.description or ""),
                        task.status.value,
                        task.created_at.isoformat(),
                        task.updated_at.isoformat(),
                    ]
                )
                rows += 1
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    tmp_path.replace(path)
    return {"file": path.name, "rows": rows}


def delete_old_exports() -> int:
    """Delete export files (and leftover temporary files) older than the retention time."""
    folder = Path(settings.EXPORT_DIR)
    cutoff = time.time() - settings.EXPORT_RETENTION_HOURS * 3600
    deleted = 0

    for pattern in ("tasks-*.csv", "tasks-*.tmp"):
        for path in folder.glob(pattern):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                deleted += 1

    return deleted


@celery_app.task(ignore_result=True)
def cleanup_exports():
    try:
        deleted = delete_old_exports()
        if deleted > 0:
            logger.info("Export cleanup deleted %s files", deleted)
        return deleted
    except Exception:
        logger.exception("Export cleanup failed")
        raise


@celery_app.task(ignore_result=True)
def mark_overdue_tasks():
    try:
        count = mark_overdue()
        if count > 0:
            logger.info("Marked %s tasks overdue", count)
        return count
    except Exception:
        logger.exception("Overdue check failed")
        raise
