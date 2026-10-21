from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from app.exceptions import (
    general_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from app.routers import cache, tasks

app = FastAPI(title="Task Manager API")

app.include_router(tasks.router)
app.include_router(cache.router)

app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)
