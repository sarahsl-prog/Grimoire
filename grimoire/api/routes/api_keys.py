"""API key management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from grimoire.api.auth import get_api_key
from grimoire.api.schemas import ApiKeyInfoResponse
from grimoire.db.models import ApiKey

router = APIRouter(prefix="/keys", tags=["api-keys"])


@router.get("/me", response_model=ApiKeyInfoResponse)
async def get_current_key_info(
    api_key: ApiKey = Depends(get_api_key),
) -> ApiKeyInfoResponse:
    """Return info about the currently authenticated API key."""
    return ApiKeyInfoResponse(
        id=api_key.id,
        name=api_key.name,
        tier=api_key.tier.value,
        prefix=api_key.key_prefix,
        expires_at=api_key.expires_at.isoformat() if api_key.expires_at else None,
        created_at=api_key.created_at.isoformat(),
    )