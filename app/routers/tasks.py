import hashlib
import json
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Response
from kombu.exceptions import OperationalError
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, col, select

from app.cache import LIST_KEY, CacheDep, TaskCache, task_key
from app.celery_jobs import export_tasks_csv
from app.config import settings
from app.database import SessionDep
from app.enums import EventType
from app.job_registry import claim_export, forget_job, release_export, remember_job
from app.messaging.publisher import add_to_outbox
from app.models.task import IDEMPOTENCY_KEY_MAX_LENGTH, Task
from app.routers import API_PREFIX
from app.schemas.job import JobRead
from app.schemas.response import ApiResponse, error_docs, ok
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate

router = APIRouter(prefix="/tasks", tags=["Tasks"])

NOT_FOUND = {"not_found": True}  # Cached in place of a task that does not exist.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def serialize_task(task: Task) -> dict:
    return TaskRead.model_validate(task).model_dump(mode="json")


def cached(cache: TaskCache, key: str, ttl: int, load: Callable[[], Any]) -> Any:
    """Cache-aside: return the cached value, or load it and cache it for ttl seconds."""
    data = cache.get(key)
    if data is None:
        token = cache.token()  # Before the database read, not after.
        data = load()
        cache.set(key, data, ttl, token)
    return data


def get_task_or_404(session: Session, task_id: UUID, lock: bool = False) -> Task:
    # lock=True reads the row with FOR UPDATE: nobody else changes it until we commit.
    task = session.get(Task, task_id, with_for_update=lock)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    return task


def flush_or_404(session: Session) -> None:
    """Send the pending change. If another request deleted the task meanwhile, answer 404."""
    try:
        session.flush()
    except StaleDataError:
        session.rollback()
        raise HTTPException(status_code=404, detail="Task not found") from None


def find_task_by_key(session: Session, key: str) -> Task | None:
    return session.exec(select(Task).where(Task.idempotency_key == key)).first()


def request_hash(task_data: TaskCreate) -> str:
    """A fingerprint of the request body. The same task data always gives the same text."""
    body = json.dumps(task_data.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def replay(task: Task, body_hash: str, response: Response) -> ApiResponse[dict]:
    """Answer a retried create with the task the first request made. Nothing new is saved."""
    # A task made before this check existed has no hash and is accepted as before.
    if task.idempotency_hash is not None and task.idempotency_hash != body_hash:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used with a different request body",
        )
    response.status_code = 200
    response.headers["Location"] = f"{API_PREFIX}/tasks/{task.id}"
    return ok(serialize_task(task), "Task already created")


# 1. CREATE TASK
@router.post(
    "",
    response_model=ApiResponse[TaskRead],
    status_code=201,
    responses=error_docs(409),
)
def create_task(
    task_data: TaskCreate,
    session: SessionDep,
    response: Response,
    cache: CacheDep,
    idempotency_key: Annotated[
        str | None, Header(min_length=1, max_length=IDEMPOTENCY_KEY_MAX_LENGTH)
    ] = None,
):
    body_hash = request_hash(task_data)

    # Same key as an earlier request: return that task instead of creating another.
    if idempotency_key is not None:
        existing = find_task_by_key(session, idempotency_key)
        if existing is not None:
            return replay(existing, body_hash, response)

    task = Task.model_validate(task_data)
    task.idempotency_key = idempotency_key
    if idempotency_key is not None:
        task.idempotency_hash = body_hash

    session.add(task)
    try:
        session.flush()
    except IntegrityError:
        # Two requests with the same key ran at the same time and the other one won.
        # The UNIQUE constraint stopped this one; answer with the winner's task.
        session.rollback()
        existing = (
            find_task_by_key(session, idempotency_key) if idempotency_key else None
        )
        if existing is None:
            raise  # Some other integrity problem, not a repeated key.
        return replay(existing, body_hash, response)
    # Read everything before the commit, so nothing can fail after the task is saved.
    payload = serialize_task(task)
    add_to_outbox(session, EventType.TASK_CREATED, payload)
    session.commit()
    cache.invalidate_tasks()

    response.headers["Location"] = f"{API_PREFIX}/tasks/{payload['id']}"
    return ok(payload, "Task created")


# 2. GET ALL TASKS
@router.get("", response_model=ApiResponse[list[TaskRead]])
def get_tasks(
    session: SessionDep,
    cache: CacheDep,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    def load() -> list[dict]:
        query = (
            select(Task)
            .order_by(col(Task.created_at), col(Task.id))  # id breaks ties
            .offset(offset)
            .limit(limit)
        )
        return [serialize_task(t) for t in session.exec(query).all()]

    # Only the default first page is cached: invalidate_tasks clears just LIST_KEY.
    is_default_page = offset == 0 and limit == DEFAULT_LIMIT
    data = (
        cached(cache, LIST_KEY, settings.LIST_CACHE_TTL, load)
        if is_default_page
        else load()
    )

    return ok(data, "Tasks retrieved")


# 3. GET ONE TASK
@router.get(
    "/{task_id}", response_model=ApiResponse[TaskRead], responses=error_docs(404)
)
def get_task(task_id: UUID, session: SessionDep, cache: CacheDep):
    key = task_key(task_id)
    data = cache.get(key)

    if data == NOT_FOUND:
        raise HTTPException(status_code=404, detail="Task not found")

    if data is None:
        token = cache.token()  # Before the database read, not after.
        task = session.get(Task, task_id)
        if task is None:
            # Remember the 404 for a few seconds, so repeated requests skip the database.
            # No need to clear it when tasks are created: ids are made by the server.
            cache.set(key, NOT_FOUND, settings.NOT_FOUND_CACHE_TTL, token)
            raise HTTPException(status_code=404, detail="Task not found")
        data = serialize_task(task)
        cache.set(key, data, settings.TASK_CACHE_TTL, token)

    return ok(data, "Task retrieved")


# 4. UPDATE TASK (PATCH: only the fields you send are changed)
@router.patch(
    "/{task_id}", response_model=ApiResponse[TaskRead], responses=error_docs(404)
)
def update_task(
    task_id: UUID,
    task_data: TaskUpdate,
    session: SessionDep,
    cache: CacheDep,
):
    # Lock the row. The overdue job skips locked rows, so it cannot set the marker for
    # the old due date while we are changing the date.
    task = get_task_or_404(session, task_id, lock=True)

    update_data = task_data.model_dump(exclude_unset=True)

    # Clear the overdue marker when due_date changes, so the task can be flagged again.
    # Check before sqlmodel_update, which overwrites the old value.
    if "due_date" in update_data and update_data["due_date"] != task.due_date:
        task.overdue_notified_at = None

    task.sqlmodel_update(update_data)

    session.add(task)
    flush_or_404(session)  # Runs the UPDATE now, so updated_at is already set.
    payload = serialize_task(task)  # Read before the commit.
    session.commit()
    cache.invalidate_tasks(task_id)

    return ok(payload, "Task updated")


# 5. DELETE TASK
@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: UUID, session: SessionDep, cache: CacheDep):
    # DELETE gives the same answer every time: a task that is already gone is a success,
    # so a client can safely retry after a lost reply.
    task = session.get(Task, task_id)

    if task is not None:
        session.delete(task)
        try:
            session.flush()
        except StaleDataError:
            session.rollback()  # Another request deleted it first. Same result.
        else:
            session.commit()

    cache.invalidate_tasks(task_id)  # Also when it was already gone: drop a stale copy.
    return Response(status_code=204)


@router.post(
    "/export",
    response_model=ApiResponse[JobRead],
    status_code=202,
    responses=error_docs(503),
)
def export_task():
    job_id = str(uuid4())
    try:
        # Only one export at a time: a second request gets the id of the one that runs.
        running = claim_export(job_id)
        if running is not None:
            return ok(
                JobRead(job_id=running, status="PENDING"), "Export already running"
            )
        # Write the id down first, then queue the job under that same id. If anything
        # fails now, nothing is left half-done: the job was either queued or it was not.
        remember_job(job_id)
        export_tasks_csv.apply_async(task_id=job_id)
    except (OperationalError, RuntimeError, RedisError) as exc:
        release_export(job_id)
        forget_job(job_id)
        raise HTTPException(status_code=503, detail="Job queue unavailable") from exc
    return ok(JobRead(job_id=job_id, status="PENDING"), "Export started")
