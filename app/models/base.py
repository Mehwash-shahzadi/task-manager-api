from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel

#helper function#
def current_time() -> datetime:
    return datetime.now(timezone.utc)


class BaseTable(SQLModel):
    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )

    created_at: datetime = Field(
        default_factory=current_time,
        sa_type=DateTime(timezone=True),
    )
   # datetime Python ka built-in type hai jo date aur time represent karta hai.
    updated_at: datetime = Field(
        default_factory=current_time,
        sa_type=DateTime(timezone=True),#DateTime btata h k database coloum m date or time ko kase handle krna h
        sa_column_kwargs={"onupdate": current_time},#opupdate sqlalchemy ki setting h jo btati h update hone pr kia krna h 
    )