import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException

from app.schemas.response import fail

logger = logging.getLogger(__name__)


def error_response(status, message, errors, headers=None):
    body = fail(message, errors).model_dump(mode="json")
    return JSONResponse(status_code=status, content=body, headers=headers)


# 1. HTTP errors (404, 403, etc.)
async def http_exception_handler(
    request: Request,
    exc: HTTPException,
):
    # detail can be a list or dict; only text is safe to show as the message.
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"

    # Keep the headers of the error (for example WWW-Authenticate or Retry-After).
    return error_response(exc.status_code, message, [message], exc.headers)


# 2. Request validation errors (422)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    errors = [
        f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors()
    ]

    return error_response(422, "Validation error", errors)


# 3. Database errors: say what kind of problem it was instead of a plain 500.
async def database_exception_handler(
    request: Request,
    exc: SQLAlchemyError,
):
    logger.error(
        "Database error while processing %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )

    if isinstance(exc, OperationalError):
        return error_response(503, "Database unavailable", ["Please try again later"])
    if isinstance(exc, IntegrityError):
        return error_response(
            409,
            "Request conflicts with existing data",
            ["The data breaks a database rule"],
        )
    return error_response(
        500, "Internal server error", ["An unexpected error occurred"]
    )


# 4. Unexpected server errors (500)
async def general_exception_handler(
    request: Request,
    exc: Exception,
):
    logger.error(
        "Unexpected error while processing %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )

    return error_response(
        500, "Internal server error", ["An unexpected error occurred"]
    )
