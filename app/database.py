from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from app.config import settings


@lru_cache
def get_engine() -> Engine:
    """The one database engine. Built on the first call, not when the module is imported."""
    return create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,  # test a connection before use, so a dead one doesn't cause a 500
        pool_size=10,
        max_overflow=30,  # 10 + 30 = 40, the same as FastAPI's default thread count
        pool_timeout=10,  # fail after 10 s instead of waiting 30 s
        pool_recycle=1800,  # replace connections older than 30 min
    )


def get_session():
    with Session(get_engine()) as session:
        yield session  # Hands the session to the endpoint and closes it when the request ends.


SessionDep = Annotated[Session, Depends(get_session)]
