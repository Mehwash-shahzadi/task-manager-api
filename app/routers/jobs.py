import logging
from pathlib import Path
from uuid import UUID

from celery.result import AsyncResult
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from redis.exceptions import RedisError

from app.celery_app import celery_app
from app.config import settings
from app.job_registry import job_exists
from app.schemas.job import JobRead
from app.schemas.response import ApiResponse, error_docs, ok

logger = logging.getLogger(__name__)

JOB_FAILED_MESSAGE = "Job failed"

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.get(
    "/{job_id}",
    response_model=ApiResponse[JobRead],
    responses=error_docs(404, 503),
)
def get_job(job_id: UUID):
    job_key = str(job_id)

    try:
        known = job_exists(job_key)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Job status unavailable") from exc
    if not known:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        result = AsyncResult(job_key, app=celery_app)
        status = result.status
        outcome = result.result
    except RedisError as exc:
        # The result store is down. Say so (503) instead of a plain 500.
        raise HTTPException(status_code=503, detail="Job status unavailable") from exc

    if status == "SUCCESS":
        job_data = JobRead(job_id=job_key, status=status, result=outcome)

    elif status == "FAILURE":
        # The real error can hold SQL, file paths or connection details, so it only goes
        # to the log. The client gets a fixed message.
        logger.error("Job %s failed: %s", job_key, outcome)
        job_data = JobRead(job_id=job_key, status=status, error=JOB_FAILED_MESSAGE)

    else:
        job_data = JobRead(job_id=job_key, status=status)

    return ok(job_data, "Job status")


@router.get(
    "/{job_id}/download", response_class=FileResponse, responses=error_docs(404)
)
def download_export(job_id: UUID):
    # job_id is a UUID, so it cannot contain "../" or any other path trick.
    path = Path(settings.EXPORT_DIR) / f"tasks-{job_id}.csv"

    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export file not found")

    return FileResponse(path, media_type="text/csv", filename=path.name)
