import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from app.exceptions import (
    general_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from app.middleware import add_process_time_header, log_requests
from app.routers import cache, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title="Task Manager API")

# Last registered is outermost: timing runs inside logging so logging can read its header
app.middleware("http")(add_process_time_header)
app.middleware("http")(log_requests)

app.include_router(tasks.router)
app.include_router(cache.router)

app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)
