import logging
import time
from uuid import uuid4

from fastapi import Request

from app.config import settings
from app.exceptions import error_response
from app.logging_config import request_id_var

logger = logging.getLogger(__name__)


async def limit_body_size(request: Request, call_next):
    # Only checks the Content-Length header. A proxy should enforce the limit too.
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > settings.MAX_BODY_BYTES:
        message = f"Request body is larger than {settings.MAX_BODY_BYTES} bytes"
        return error_response(413, "Request body too large", [message])
    return await call_next(request)


async def log_requests(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("X-Request-ID") or uuid4().hex[:12]
    request_id_var.set(request_id)

    try:
        response = await call_next(request)
    except Exception:
        # Handle it here, inside the middleware, so the 500 also gets the request id and timing.
        logger.exception(
            "Unexpected error while processing %s %s", request.method, request.url.path
        )
        response = error_response(
            500, "Internal server error", ["An unexpected error occurred"]
        )

    process_time = f"{time.perf_counter() - start_time:.4f}"
    response.headers["X-Process-Time"] = process_time
    response.headers["X-Request-ID"] = request_id

    logger.info(
        "%s %s %s %ss",
        request.method,
        request.url.path,
        response.status_code,
        process_time,
    )
    return response
