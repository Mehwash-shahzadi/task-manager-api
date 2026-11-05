from celery import Celery
from celery.signals import worker_process_init

from app.config import settings
from app.database import get_engine

CLEANUP_EVERY_SECONDS = 3600.0
OVERDUE_EVERY_SECONDS = 60.0
STREAM_CHECK_EVERY_SECONDS = 60.0

celery_app = Celery(
    "task_manager",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.celery_jobs"],
)


# Give each forked worker process its own database connections.
@worker_process_init.connect
def dispose_engine_after_fork(**kwargs):
    get_engine().dispose(close=False)


celery_app.conf.update(
    # Tasks and results are serialized as JSON.
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Time settings
    timezone="UTC",
    enable_utc=True,
    # Task execution settings
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    # Results expire after 1 hour.
    result_expires=settings.JOB_RESULT_TTL_SECONDS,
    # Retry the broker connection at startup.
    broker_connection_retry_on_startup=True,
    result_backend_transport_options={
        "retry_policy": {
            "max_retries": 2,
            "interval_start": 0,
            "interval_step": 0.2,
            "interval_max": 0.5,
        }
    },
    # Scheduled tasks
    beat_schedule={
        "cleanup-outbox": {
            "task": "app.celery_jobs.cleanup_outbox",
            "schedule": CLEANUP_EVERY_SECONDS,
        },
        "cleanup-exports": {
            "task": "app.celery_jobs.cleanup_exports",
            "schedule": CLEANUP_EVERY_SECONDS,
        },
        "relay-outbox": {
            "task": "app.celery_jobs.relay_outbox",
            "schedule": settings.OUTBOX_POLL_SECONDS,
            "options": {
                "expires": 10,  # Expire old, unexecuted Celery task messages
            },
        },
        "watch-stream": {
            "task": "app.celery_jobs.watch_stream",
            "schedule": STREAM_CHECK_EVERY_SECONDS,
            "options": {"expires": 50},
        },
        "watch-outbox": {
            "task": "app.celery_jobs.watch_outbox",
            "schedule": STREAM_CHECK_EVERY_SECONDS,
            "options": {"expires": 50},
        },
        "mark-overdue-tasks": {
            "task": "app.celery_jobs.mark_overdue_tasks",
            "schedule": OVERDUE_EVERY_SECONDS,
            "options": {"expires": 50},
        },
    },
)
