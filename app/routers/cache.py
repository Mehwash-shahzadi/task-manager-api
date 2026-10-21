import json

from fastapi import APIRouter

from app.cache import PREFIX, redis_client
from app.schemas.cache import CacheEntry
from app.schemas.response import ApiResponse

router = APIRouter(prefix="/cache", tags=["Cache"])


@router.get("", response_model=ApiResponse[list[CacheEntry]])
def get_cache():
    entries = []

    # scan_iter uses SCAN, not KEYS, and the prefix keeps it to this app's keys
    for key in redis_client.scan_iter(match=f"{PREFIX}*"):
        value = redis_client.get(key)
        if value is None:  # expired between SCAN and GET
            continue
        entries.append(
            CacheEntry(key=key, ttl=redis_client.ttl(key), value=json.loads(value))
        )

    return ApiResponse[list[CacheEntry]](
        success=True,
        message="Cache entries",
        data=entries,
    )
