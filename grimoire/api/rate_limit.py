"""Rate limiting for Grimoire.

Three stages, because slowapi's middleware runs before authentication and so
can never see which API key (or tier) a request belongs to. Previously every
request, keyed or not, shared one per-IP bucket of 30/minute; agent keys,
documented at 600/minute, were throttled at 30.

1. **Pre-auth, per client IP** (slowapi middleware). Requests without an
   ``X-API-Key`` header get ``DEFAULT_LIMIT``. Requests with one get a ceiling
   equal to the highest tier limit, so no tier is throttled here, while a
   single IP still can't make the API do unbounded key lookups.
2. **Per API key** (:func:`enforce_tier_limit`, called by the auth dependency
   once the key is verified): the key's tier limit from
   ``auth.rate_limits``.
3. **Failed authentication, per client IP** (:func:`check_failed_auth` /
   :func:`record_failed_auth`): ``auth.failed_auth_limit`` bad keys per
   window. Past it the key isn't even looked up, so guessing keys can't burn
   bcrypt time.

All three share one storage: Redis when configured, with slowapi's in-memory
fallback when Redis is unreachable.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from limits import RateLimitItem, parse
from loguru import logger
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.types import ASGIApp, Receive, Scope, Send

if TYPE_CHECKING:
    from grimoire.config.settings import AuthConfig
    from grimoire.db.models import ApiKey

DEFAULT_LIMIT = "30/minute"

# Whether the current request carries an X-API-Key header. Set by
# _ApiKeyPresenceMiddleware before slowapi runs. slowapi evaluates default
# limits without passing the request to the limit provider, so the provider
# reads this instead.
_has_api_key: ContextVar[bool] = ContextVar("grimoire_has_api_key", default=False)


class _ApiKeyPresenceMiddleware:
    """Record whether the request sent an API key, for the pre-auth limit.

    Pure ASGI (like the app's other outer middleware), so SSE streams on
    ``/mcp`` pass straight through.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        has_key = any(
            name == b"x-api-key" and value.strip()
            for name, value in scope.get("headers", [])
        )
        token = _has_api_key.set(has_key)
        try:
            await self.app(scope, receive, send)
        finally:
            _has_api_key.reset(token)


def _get_client_ip(request: Request) -> str:
    """Key by client IP only; the liveness probe is unauthenticated."""
    return get_remote_address(request) or "anonymous"


def create_health_limiter() -> Limiter:
    """Build the in-memory limiter that guards ``/health``.

    In-memory on purpose: ``/health`` must keep answering when Redis is down, so
    its own limit (code review W-7) cannot live in the Redis-backed limiter.
    The counters are per process, which is fine for a probe limit.

    One limiter per app, not a module global: ``Limiter.limit`` registers the
    route on the instance each time it is applied, so a shared instance would
    stack a duplicate limit (and double-count every hit) for each
    ``create_app()`` call.
    """
    return Limiter(key_func=_get_client_ip, storage_uri="memory://")


def _preauth_key(request: Request) -> str:
    """Per-IP bucket, split by whether an API key was sent.

    Separate buckets so a client's unauthenticated probes and its keyed calls
    don't share one budget.
    """
    kind = "keyed" if _has_api_key.get() else "anon"
    return f"preauth:{kind}:{_get_client_ip(request)}"


def _highest_limit(limits: dict[str, str]) -> str:
    """The most permissive limit (highest requests per second) of ``limits``."""

    def per_second(text: str) -> float:
        item = parse(text)
        return item.amount / item.get_expiry()

    return max(limits.values(), key=per_second, default=DEFAULT_LIMIT)


def _auth_config() -> AuthConfig:
    from grimoire.config.settings import get_settings

    return get_settings().auth


def setup_rate_limiting(app: FastAPI) -> Limiter:
    """Configure rate limiting on the FastAPI app.

    Creates a Limiter backed by Redis (if configured) with an in-memory
    fallback, adds SlowAPIMiddleware (stage 1) and the API-key presence
    middleware it relies on, and stores the limiter on ``app.state.limiter``,
    where the auth dependency finds it for stages 2 and 3.

    Returns:
        The configured Limiter instance.
    """
    storage_uri = None
    keyed_ceiling = DEFAULT_LIMIT

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
        keyed_ceiling = _highest_limit(settings.auth.rate_limits)
    except Exception as e:
        # Only reached when settings fail to load; Redis reachability is
        # handled per request by the in-memory fallback below.
        # Log the type only: a settings ValidationError can echo input values,
        # which may include the Redis password.
        logger.warning(
            "Rate limiting using in-memory storage "
            f"(settings unavailable: {type(e).__name__})"
        )

    def _preauth_limit() -> str:
        return keyed_ceiling if _has_api_key.get() else DEFAULT_LIMIT

    # in_memory_fallback_enabled: without it, a Redis outage makes slowapi's
    # middleware pass the redis ConnectionError to its 429 handler, which
    # expects RateLimitExceeded and crashes reading `.detail`, so every
    # request except /health returns 500. With it, slowapi logs a warning,
    # enforces the same limits from per-process memory, and re-checks Redis
    # with exponential backoff until it recovers.
    limiter = Limiter(
        key_func=_preauth_key,
        default_limits=[_preauth_limit],
        storage_uri=storage_uri,
        in_memory_fallback_enabled=True,
    )

    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    # Added after, so it wraps SlowAPIMiddleware and sets the flag first.
    app.add_middleware(_ApiKeyPresenceMiddleware)
    return limiter


# ---------------------------------------------------------------------------
# Stages 2 and 3, called from grimoire.api.auth
# ---------------------------------------------------------------------------


def _active_limiter(request: Request) -> Any | None:
    """The app's limiter, or None when rate limiting is off (e.g. in tests)."""
    limiter = getattr(request.app.state, "limiter", None)
    if limiter is None or not getattr(limiter, "enabled", True):
        return None
    return limiter


def _call(limiter: Any, op: str, item: RateLimitItem, *keys: str) -> Any:
    """Run ``hit``/``test``/``get_window_stats`` with slowapi's memory fallback.

    slowapi only switches to its in-memory fallback when its own middleware
    check fails, and that never runs for ``/mcp`` (a mounted app has no
    endpoint, so slowapi skips it). Without this, a Redis outage would make
    these stages fail open, including the invalid-key cap.
    """
    try:
        return getattr(limiter.limiter, op)(item, *keys)
    except Exception as e:
        fallback = getattr(limiter, "_fallback_limiter", None)
        if fallback is None:
            raise
        logger.warning(
            f"Rate limit storage unavailable ({type(e).__name__}); "
            "using in-memory counters"
        )
        return getattr(fallback, op)(item, *keys)


def _too_many(item: RateLimitItem, what: str, limiter: Any, *keys: str) -> None:
    """Raise a 429 for ``item`` with a Retry-After from the window stats."""
    headers: dict[str, str] = {}
    try:
        reset_at, _ = _call(limiter, "get_window_stats", item, *keys)
        headers["Retry-After"] = str(max(1, int(reset_at - time.time())))
    except Exception as e:
        # Retry-After is a courtesy; the 429 stands without it.
        logger.debug(f"No Retry-After for rate limit: {type(e).__name__}")
    raise HTTPException(
        status_code=429, detail=f"Rate limit exceeded: {what} ({item})", headers=headers
    )


def enforce_tier_limit(request: Request, api_key: ApiKey) -> None:
    """Count this request against the key's tier limit; 429 when exceeded.

    Uses in-memory counters if Redis is down (see :func:`_call`); fails open
    only if those error too, since refusing every authenticated request would
    be worse.
    """
    limiter = _active_limiter(request)
    if limiter is None:
        return
    tier = api_key.tier.value
    item = parse(_auth_config().rate_limits.get(tier, DEFAULT_LIMIT))
    keys = ("tier", api_key.key_prefix)
    try:
        allowed = _call(limiter, "hit", item, *keys)
    except Exception as e:
        logger.warning(f"Tier rate limit check failed ({type(e).__name__}); allowing")
        return
    if not allowed:
        _too_many(item, f"{tier} tier limit for this API key", limiter, *keys)


def _failed_auth_item() -> RateLimitItem:
    return parse(_auth_config().failed_auth_limit)


def check_failed_auth(request: Request) -> None:
    """Refuse up front if this IP has used up its failed-auth allowance.

    Runs before the key lookup, so a client guessing keys stops costing a
    database query and a bcrypt check once over the limit.
    """
    limiter = _active_limiter(request)
    if limiter is None:
        return
    item = _failed_auth_item()
    keys = ("authfail", _get_client_ip(request))
    try:
        within = _call(limiter, "test", item, *keys)
    except Exception as e:
        logger.warning(f"Failed-auth limit check failed ({type(e).__name__})")
        return
    if not within:
        _too_many(item, "too many invalid API keys from this address", limiter, *keys)


def record_failed_auth(request: Request) -> None:
    """Count one invalid API key against this IP's failed-auth allowance."""
    limiter = _active_limiter(request)
    if limiter is None:
        return
    try:
        _call(limiter, "hit", _failed_auth_item(), "authfail", _get_client_ip(request))
    except Exception as e:
        logger.warning(f"Failed-auth limit update failed ({type(e).__name__})")
