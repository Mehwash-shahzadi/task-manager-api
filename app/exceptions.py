import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.schemas.response import ApiResponse


logger = logging.getLogger(__name__)


# 1. HTTP errors (404, 403, etc.)
async def http_exception_handler(
    request: Request,
    exc: HTTPException,
):
    message = str(exc.detail)

    response = ApiResponse[None](
        success=False,
        message=message,
        data=None,
        errors=[message],
    )

    return JSONResponse(
        status_code=exc.status_code,
        content=response.model_dump(mode="json")#model_dump response ko json m meam dictionary m convert krr k response dein ga 
    )


# 2. Request validation errors (422)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
):
    errors = [
        f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
        for error in exc.errors()
    ]

    response = ApiResponse[None](
        success=False,
        message="Validation error",
        data=None,
        errors=errors,
    )

    return JSONResponse(
        status_code=422,
        content=response.model_dump(mode="json"),
    )


# 3. Unexpected server errors (500)
async def general_exception_handler(
    request: Request,
    exc: Exception,
):
    logger.error(
        "Unexpected error while processing %s %s",
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )

    response = ApiResponse[None](
        success=False,
        message="Internal server error",
        data=None,
        errors=["An unexpected error occurred"],
    )

    return JSONResponse(
        status_code=500,
        content=response.model_dump(mode="json"),
    )