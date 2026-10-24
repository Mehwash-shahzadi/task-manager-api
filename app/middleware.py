import logging
import time

from fastapi import Request

logger = logging.getLogger(__name__)


async def add_process_time_header(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time = time.perf_counter() - start_time
    response.headers["X-Process-Time"] = f"{process_time:.4f}"
    return response


async def log_requests(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        # The global Exception handler runs outside middleware, so log here and re-raise
        logger.exception("%s %s 500", request.method, request.url.path)
        raise

    process_time = response.headers.get("X-Process-Time", "n/a")
    logger.info(
        "%s %s %s %ss",
        request.method,
        request.url.path,
        response.status_code,
        process_time,
    )
    return response
