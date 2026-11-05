from fastapi import APIRouter, Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session
from starlette.exceptions import HTTPException

from app.cache import cache_stats
from app.config import settings
from app.database import get_engine
from app.exceptions import (
    database_exception_handler,
    general_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from app.logging_config import setup_logging
from app.messaging.monitor import dead_count, pending_count, stream_backlog
from app.middleware import limit_body_size, log_requests
from app.redis_client import get_cache_client, get_redis_client
from app.routers import API_PREFIX, cache, jobs, tasks
from app.schemas.response import error_docs
from app.security import require_api_key

setup_logging()


def create_app() -> FastAPI:
    """Build the app. Tests can call this to get a fresh app with their own settings."""
    app = FastAPI(
        title="Task Manager API",
        docs_url="/docs" if settings.DOCS_ENABLED else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.DOCS_ENABLED else None,
    )

    @app.get("/health")
    def health_check():
        try:
            with Session(get_engine()) as session:
                session.exec(text("SELECT 1"))

            get_redis_client().ping()
            get_cache_client().ping()

            return {
                "status": "healthy",
                "database": "connected",
                "redis": "connected",
                "outbox_pending": pending_count(),
                "outbox_dead": dead_count(),
                "stream_backlog": stream_backlog(),
                "cache": cache_stats(),
            }

        except (SQLAlchemyError, RedisError) as exc:
            raise HTTPException(
                status_code=503,
                detail="A required service is unavailable",
            ) from exc

    # Last registered is outermost: log_requests wraps everything, so it also logs and
    # times the 413 that limit_body_size returns.
    app.middleware("http")(limit_body_size)
    app.middleware("http")(log_requests)

    # Every router needs the API key. /health is defined above and stays open, because
    # the Docker healthcheck calls it without a key.
    api = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(require_api_key)],
        responses=error_docs(401, 413, 422, 500),  # Can happen on any route.
    )
    api.include_router(tasks.router)
    api.include_router(cache.router)
    api.include_router(jobs.router)
    app.include_router(api)

    # Exception handlers: return every error in the same envelope.
    # HTTPException (404 etc.), RequestValidationError (422), database errors, any other
    # Exception (500). Starlette's hint asks for a handler of Exception, but it calls each
    # handler with the matching error type, so the narrower hints on our handlers are correct.
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        validation_exception_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        SQLAlchemyError,
        database_exception_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(Exception, general_exception_handler)

    return app


app = create_app()
