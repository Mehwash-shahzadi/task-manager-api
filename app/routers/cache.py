from fastapi import APIRouter

from app.cache import cache_list
from app.schemas.cache import CacheEntry
from app.schemas.response import ApiResponse, ok

router = APIRouter(prefix="/cache", tags=["Cache"])


@router.get("", response_model=ApiResponse[list[CacheEntry]])
def get_cache():
    return ok(cache_list(), "Cache entries")
