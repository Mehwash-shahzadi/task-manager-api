from typing import Any

from sqlmodel import SQLModel


class JobRead(SQLModel):
    job_id: str
    result: Any | None = None
    error: str | None = None
    status: str
