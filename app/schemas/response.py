from typing import Generic, TypeVar

from pydantic import BaseModel

# TypeVar is a placeholder that can stand for any data type.
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


class ErrorResponse(ApiResponse[None]):
    """The answer for every error: success is false and `errors` lists what went wrong."""


ERROR_DESCRIPTIONS = {
    401: "Missing or wrong X-API-Key",
    404: "Not found",
    409: "The request conflicts with existing data",
    413: "Request body too large",
    422: "Validation error",
    500: "Internal server error",
    503: "A required service is unavailable",
}


def error_docs(*codes: int) -> dict[int | str, dict]:
    """The `responses=` argument for a route, so /docs shows our real error answer."""
    return {
        code: {"model": ErrorResponse, "description": ERROR_DESCRIPTIONS[code]}
        for code in codes
    }
