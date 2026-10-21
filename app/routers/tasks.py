
from uuid import UUID
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.cache import LIST_KEY, cache_get, cache_invalidate, cache_set, task_key
from app.database import get_session
from app.models.task import Task
from app.schemas.task import TaskCreate, TaskUpdate, TaskRead
from app.schemas.response import ApiResponse


router = APIRouter(prefix="/tasks", tags=["Tasks"])
#Annotated[...] → Type information aur FastAPI ki dependency instructions ko ek saath likhne deta hai.
SessionDep = Annotated[Session, Depends(get_session)]


# 1. CREATE TASK
@router.post(
    "",
    response_model=ApiResponse[TaskRead],
    status_code=201,
)
def create_task(task_data: TaskCreate, session: SessionDep):
    task = Task.model_validate(task_data)

    session.add(task)
    session.commit()
    session.refresh(task)
    cache_invalidate(LIST_KEY)

    return ApiResponse[TaskRead](
        success=True,
        message="Task created",
        data=task,
    )


# 2. GET ALL TASKS
@router.get("", response_model=ApiResponse[list[TaskRead]])
def get_tasks(session: SessionDep):
    data = cache_get(LIST_KEY)

    if data is None:
        tasks = session.exec(select(Task)).all()
        data = [TaskRead.model_validate(t).model_dump(mode="json") for t in tasks]
        cache_set(LIST_KEY, data)

    return ApiResponse[list[TaskRead]](
        success=True,
        message="Tasks retrieved",
        data=data,
    )


# 3. GET ONE TASK
@router.get("/{id}", response_model=ApiResponse[TaskRead])
def get_task(id: UUID, session: SessionDep):
    data = cache_get(task_key(id))

    if data is None:
        task = session.get(Task, id)

        if task is None:
            raise HTTPException(
                status_code=404,
                detail="Task not found",
            )

        data = TaskRead.model_validate(task).model_dump(mode="json")
        cache_set(task_key(id), data)

    return ApiResponse[TaskRead](
        success=True,
        message="Task retrieved",
        data=data,
    )


# 4. UPDATE TASK
@router.put("/{id}", response_model=ApiResponse[TaskRead])
def update_task(
    id: UUID,
    task_data: TaskUpdate,
    session: SessionDep,
):
    task = session.get(Task, id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    update_data = task_data.model_dump(exclude_unset=True)

    # These two fields cannot be null in the database.
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

    return ApiResponse[TaskRead](
        success=True,
        message="Task updated",
        data=task,
    )


# 5. DELETE TASK
@router.delete("/{id}", response_model=ApiResponse[None])
def delete_task(id: UUID, session: SessionDep):
    task = session.get(Task, id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="Task not found",
        )

    session.delete(task)
    session.commit()
    cache_invalidate(LIST_KEY, task_key(id))

    return ApiResponse[None](
        success=True,
        message="Task deleted",
        data=None,
    )
