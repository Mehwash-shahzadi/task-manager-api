from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, FetchedValue, func
from sqlmodel import Field, SQLModel

# Fixed names for constraints and indexes, so migrations can refer to them.
SQLModel.metadata.naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# Current time in UTC.
def current_time() -> datetime:
    return datetime.now(UTC)


class BaseTable(SQLModel):
    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )

    # The database sets both times (server_default on insert, a trigger on update), so
    # every row uses the one database clock and updated_at is never earlier than
    # created_at. default=None means "not set yet": the ORM leaves the column out of the
    # INSERT and reads the value back from the database.
    created_at: datetime = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": func.now()},
    )
    updated_at: datetime = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={
            "server_default": func.now(),
            # The trigger changes this column on every UPDATE. FetchedValue tells the ORM
            # to read the new value back, so the answer shows the real updated_at.
            "server_onupdate": FetchedValue(),
        },
    )
