"""Rate limiting for Grimoire using slowapi.

Integrates slowapi with FastAPI to provide per-tier rate limiting
backed by Redis. Tier-specific limits are applied based on the
authenticated API key; unauthenticated requests get a default limit.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from grimoire.api.auth import DEFAULT_TIER_RATE_LIMITS

DEFAULT_LIMIT = "30/minute"


def _get_rate_limit_key(request: Request) -> str:
    """Derive the rate limit key from the authenticated API key or IP.

    Uses tier+prefix for authenticated requests so each key gets its
    own bucket. Falls back to client IP for unauthenticated requests.
    """
    api_key = getattr(request.state, "api_key", None)
    if api_key:
        return f"{api_key.tier.value}:{api_key.key_prefix}"
    return get_remote_address(request) or "anonymous"


def get_tier_rate_limit(tier_code: str) -> str:
    """Return the slowapi limit string for a tier code."""
    return DEFAULT_TIER_RATE_LIMITS.get(tier_code, DEFAULT_LIMIT)


def setup_rate_limiting(app: FastAPI) -> Limiter:
    """Configure slowapi rate limiting on the FastAPI app.

    Creates a Limiter backed by Redis (if configured) or in-memory
    storage, adds SlowAPIMiddleware, and stores the limiter on
    app.state.limiter for use in route decorators.

    Returns:
        The configured Limiter instance.
    """
    storage_uri = None

    try:
        from grimoire.config.settings import get_settings

        settings = get_settings()
        redis_url = (
            f"redis://{settings.redis.host}:{settings.redis.port}"
            f"/{settings.redis.db_rate_limit}"
        )
        if settings.redis.password:
            redis_url = f"redis://:{settings.redis.password}@{settings.redis.host}:{settings.redis.port}/{settings.redis.db_rate_limit}"
        storage_uri = redis_url
    except Exception:
        from loguru import logger

        logger.debug("Rate limiting falling back to in-memory storage (Redis unavailable)")

    limiter = Limiter(
        key_func=_get_rate_limit_key,
        default_limits=[DEFAULT_LIMIT],
        storage_uri=storage_uri,
    )

    # SlowAPIMiddleware reads the limiter from app.state.limiter
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    return limiter