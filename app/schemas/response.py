from typing import Generic, TypeVar
from pydantic import BaseModel

# TypeVar ek dobara use hone wala type placeholder banata h jo alag alag data types ki jagah le sakta h
DataT = TypeVar("DataT")


class ApiResponse(BaseModel, Generic[DataT]):
    success: bool
    message: str
    data: DataT | None = None
    errors: list[str] | None = None


def ok(data: DataT | None, message: str) -> ApiResponse[DataT]:
    return ApiResponse[DataT](success=True, message=message, data=data)


def fail(message: str, errors: list[str]) -> ApiResponse[None]:
    return ApiResponse[None](success=False, message=message, data=None, errors=errors)
