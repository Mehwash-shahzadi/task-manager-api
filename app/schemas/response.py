from typing import Generic, TypeVar
from pydantic import BaseModel

# TypeVar creates a reusable type placeholder that can represent different data types.
DataT = TypeVar("DataT")


class ApiResponse(BaseModel, Generic[DataT]):
    success: bool
    message: str
    data: DataT | None = None
    errors: list[str] | None = None