"""API key authentication for Grimoire.

Provides tiered API key generation, bcrypt-hashed storage, and
FastAPI dependency injection for authenticating requests via the
X-API-Key header.

Key format: grim_{tier_code}_{random}
  - agt: agent tier (high rate limit)
  - dvl: dev tier (moderate rate limit)
  - rdl: read tier (low rate limit)

TODO: Future optimization -- cache ApiKey lookups in Redis with a short
TTL (e.g. 60s) to avoid DB round-trips on every request. This becomes
worthwhile when request volume makes the per-request SELECT+bcrypt
a measurable bottleneck.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.dependencies import get_db_session
from grimoire.db.models import ApiKey, ApiKeyTier

# FastAPI security scheme — reads X-API-Key header
api_key_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)

# Default rate limits per tier (can be overridden via settings)
DEFAULT_TIER_RATE_LIMITS: dict[str, str] = {
    "agt": "600/minute",
    "dvl": "120/minute",
    "rdl": "30/minute",
}

# Prefix for all API keys
KEY_PREFIX = "grim_"

# Length of the key_prefix column (first N chars stored for fast lookup)
KEY_PREFIX_LENGTH = 12


def generate_api_key(tier: ApiKeyTier) -> tuple[str, str, str]:
    """Generate a new API key for the given tier.

    Returns:
        Tuple of (raw_key, key_prefix, key_hash).
        raw_key is shown once and never stored.
        key_prefix is the first 12 chars for fast DB lookup.
        key_hash is the bcrypt hash of the full key.
    """
    random_part = secrets.token_urlsafe(32)
    raw_key = f"{KEY_PREFIX}{tier.value}_{random_part}"
    key_prefix = raw_key[:KEY_PREFIX_LENGTH]
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode()
    return raw_key, key_prefix, key_hash


async def authenticate_api_key(
    raw_key: str, db: AsyncSession
) -> ApiKey | None:
    """Authenticate an API key against the database.

    Looks up the key by prefix (first 12 chars), verifies the bcrypt
    hash, and checks that the key is neither revoked nor expired.
    """
    if not raw_key or not raw_key.startswith(KEY_PREFIX):
        return None

    key_prefix = raw_key[:KEY_PREFIX_LENGTH]

    stmt = select(ApiKey).where(
        ApiKey.key_prefix == key_prefix,
        ApiKey.revoked_at.is_(None),
    )
    result = await db.execute(stmt)
    api_key = result.scalar_one_or_none()

    if api_key is None:
        return None

    # Verify bcrypt hash
    try:
        if not bcrypt.checkpw(raw_key.encode(), api_key.key_hash.encode()):
            return None
    except (ValueError, TypeError):
        return None

    # Check expiration
    if api_key.expires_at is not None and api_key.expires_at <= datetime.now(
        timezone.utc
    ):
        return None

    # Update last_used_at (fire-and-forget, don't block the request)
    api_key.last_used_at = datetime.now(timezone.utc)
    try:
        await db.flush()
    except Exception:
        # Logging update failure should not torch an already-authenticated request
        pass

    return api_key


async def get_api_key(
    request: Request,
    raw_key: str = Depends(api_key_header_scheme),
    db: AsyncSession = Depends(get_db_session),
) -> ApiKey:
    """FastAPI dependency that authenticates the request via X-API-Key.

    Raises HTTPException 401 if the key is missing or invalid.
    Sets request.state.api_key for downstream use (e.g. rate limiting).
    """
    if raw_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Pass X-API-Key header.",
        )

    api_key = await authenticate_api_key(raw_key, db)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key.",
        )

    request.state.api_key = api_key
    return api_key