import secrets

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import settings

# auto_error=False: we raise the 401 ourselves, so the answer has our usual error envelope.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str | None = Security(api_key_header)) -> None:
    """Let the request through only if X-API-Key matches the API_KEY setting."""
    # An empty API_KEY setting must never let an empty header through.
    expected = settings.API_KEY
    # compare_digest takes the same time however many characters match (no timing leak).
    # It needs bytes here, because a str with non-ASCII letters would raise TypeError.
    if (
        not expected
        or not key
        or not secrets.compare_digest(key.encode(), expected.encode())
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
