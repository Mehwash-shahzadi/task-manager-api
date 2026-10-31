
from uuid import UUID
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.cache import LIST_KEY, cache_get, cache_invalidate, cache_set, task_key
from app.database import get_session
from app.events import add_to_outbox
from app.models.task import Task
from app.schemas.task import TaskCreate, TaskUpdate, TaskRead
from app.schemas.response import ApiResponse, ok


router = APIRouter(prefix="/tasks", tags=["Tasks"])
#Annotated[...] → Type information aur FastAPI ki dependency instructions ko ek saath likhne deta hai.
SessionDep = Annotated[Session, Depends(get_session)]


def get_task_or_404(session: Session, id: UUID) -> Task:
    task = session.get(Task, id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    return task


# 1. CREATE TASK
@router.post(
    "",
    response_model=ApiResponse[TaskRead],
    status_code=201,
)
def create_task(task_data: TaskCreate, session: SessionDep):
    task = Task.model_validate(task_data)

    session.add(task)
    session.flush()
    add_to_outbox(session, "task.created", TaskRead.model_validate(task).model_dump(mode="json"))
    session.commit()
    session.refresh(task)
    cache_invalidate(LIST_KEY)

    return ok(task, "Task created")


# 2. GET ALL TASKS
@router.get("", response_model=ApiResponse[list[TaskRead]])
def get_tasks(session: SessionDep):
    data = cache_get(LIST_KEY)

    if data is None:
        tasks = session.exec(select(Task)).all()
        data = [TaskRead.model_validate(t).model_dump(mode="json") for t in tasks]
        cache_set(LIST_KEY, data)

    return ok(data, "Tasks retrieved")


# 3. GET ONE TASK
@router.get("/{id}", response_model=ApiResponse[TaskRead])
def get_task(id: UUID, session: SessionDep):
    data = cache_get(task_key(id))

    if data is None:
        task = get_task_or_404(session, id)
        data = TaskRead.model_validate(task).model_dump(mode="json")
        cache_set(task_key(id), data)

    return ok(data, "Task retrieved")


# 4. UPDATE TASK
@router.put("/{id}", response_model=ApiResponse[TaskRead])
def update_task(
    id: UUID,
    task_data: TaskUpdate,
    session: SessionDep,
):
    task = get_task_or_404(session, id)

    update_data = task_data.model_dump(exclude_unset=True)

    # ye dono fields database mein null nahi ho sakti
    if update_data.get("title", "valid") is None:
        raise HTTPException(
            status_code=422,
            detail="Title cannot be null",
        )

    if update_data.get("status", "valid") is None:
        raise HTTPException(
            status_code=422,
            detail="Status cannot be null",
        )

    task.sqlmodel_update(update_data)

    session.add(task)
    session.commit()
    session.refresh(task)
    cache_invalidate(LIST_KEY, task_key(id))

    return ok(task, "Task updated")


# 5. DELETE TASK
@router.delete("/{id}", response_model=ApiResponse[None])
def delete_task(id: UUID, session: SessionDep):
    task = get_task_or_404(session, id)

    session.delete(task)
    session.commit()
    cache_invalidate(LIST_KEY, task_key(id))

    return ok(None, "Task deleted")
