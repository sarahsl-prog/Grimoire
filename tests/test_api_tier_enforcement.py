"""REST write endpoints enforce the same key tiers as the MCP server.

Before this, ``grim_rdl_*`` (read) keys could delete documents, ingest files and
start watchers over REST: tiers only changed the rate limit. Two kinds of test
here: a matrix of what each tier may call, and a guard that fails when someone
adds a new write route without gating it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from grimoire.api.auth import get_api_key
from grimoire.api.dependencies import get_db_session
from grimoire.api.main import create_app
from grimoire.db.models import ApiKey, ApiKeyTier

NOW = datetime(2026, 1, 1, tzinfo=UTC)
A, D, R = ApiKeyTier.AGENT, ApiKeyTier.DEV, ApiKeyTier.READ

# (method, path, lowest tier allowed).  Paths use throwaway ids: an allowed call
# may legitimately fail further in (404, 422, 500 against the stub database);
# the only thing asserted is whether the *tier gate* let it through.
WRITE_MATRIX: list[tuple[str, str, ApiKeyTier]] = [
    ("POST", "/api/v1/ingest/file", D),
    ("POST", "/api/v1/ingest/directory", D),
    ("POST", "/api/v1/ingest/upload", D),
    ("POST", "/api/v1/generate", D),
    ("POST", "/api/v1/categories", D),
    ("POST", "/api/v1/watch/start", D),
    ("DELETE", "/api/v1/watch/w-1", D),
    (
        "PUT",
        "/api/v1/documents/a0000000-0000-4000-8000-000000000001/tags/b0000000-0000-4000-8000-000000000001",
        D,
    ),
    (
        "DELETE",
        "/api/v1/documents/a0000000-0000-4000-8000-000000000001/tags/b0000000-0000-4000-8000-000000000001",
        D,
    ),
    ("DELETE", "/api/v1/documents/a0000000-0000-4000-8000-000000000001", A),
    ("DELETE", "/api/v1/categories/a0000000-0000-4000-8000-000000000001", A),
]

# POST endpoints that only read; any valid key may call them.
READ_ONLY_POSTS = {("POST", "/api/v1/query/ask"), ("POST", "/api/v1/query/search")}


def _key(tier: ApiKeyTier) -> ApiKey:
    return ApiKey(
        id="k",
        name="k",
        tier=tier,
        key_prefix=f"grim_{tier.value}_tst",
        key_hash="x",
        created_at=NOW,
    )


def _client(tier: ApiKeyTier) -> TestClient:
    app = create_app(use_lifespan=False)
    app.state.limiter.enabled = False  # rate limiting needs Redis; covered elsewhere

    async def stub_db() -> AsyncIterator[Any]:
        session = MagicMock()
        session.get = AsyncMock(return_value=None)
        session.execute = AsyncMock(side_effect=RuntimeError("stub database"))
        yield session

    async def key() -> ApiKey:
        return _key(tier)

    app.dependency_overrides[get_db_session] = stub_db
    app.dependency_overrides[get_api_key] = key
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(("method", "path", "minimum"), WRITE_MATRIX)
@pytest.mark.parametrize("held", [R, D, A])
def test_write_endpoints_follow_the_tier_matrix(
    method: str, path: str, minimum: ApiKeyTier, held: ApiKeyTier
) -> None:
    rank = {R: 0, D: 1, A: 2}
    response = _client(held).request(method, path, json={})

    if rank[held] < rank[minimum]:
        assert response.status_code == 403, (method, path, held, response.text)
    else:
        assert response.status_code != 403, (method, path, held, response.text)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/documents"),
        ("GET", "/api/v1/categories"),
        ("GET", "/api/v1/watch/status"),
        ("GET", "/api/v1/keys/me"),
        ("POST", "/api/v1/query/ask"),
        ("POST", "/api/v1/query/search"),
    ],
)
def test_a_read_key_can_still_read(method: str, path: str) -> None:
    response = _client(R).request(method, path, json={})
    assert response.status_code != 403, (method, path, response.text)


def _all_api_routes() -> list[APIRoute]:
    """Every route, found through the router modules.

    Not through ``app.routes``: recent FastAPI wraps an included router in an
    opaque object there, which makes a walk of the app silently find nothing.
    """
    import importlib
    import pkgutil

    import grimoire.api.routes as package

    routes: list[APIRoute] = []
    for info in pkgutil.iter_modules(package.__path__):
        router = getattr(
            importlib.import_module(f"{package.__name__}.{info.name}"), "router", None
        )
        if router is not None:
            routes.extend(r for r in router.routes if isinstance(r, APIRoute))
    return routes


def _is_gated(route: APIRoute) -> bool:
    return any(
        getattr(dep.call, "min_tier", None) is not None
        for dep in route.dependant.dependencies
    )


def test_every_mutating_route_is_gated_or_explicitly_read_only() -> None:
    """A new POST/PUT/PATCH/DELETE route must pick a tier, not default to open."""
    mutating = {
        (method, f"/api/v1{route.path}"): route
        for route in _all_api_routes()
        for method in route.methods
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    # Not vacuous: the matrix plus the read-only POSTs are all really found.
    assert len(mutating) >= len(WRITE_MATRIX) + len(READ_ONLY_POSTS)

    ungated = {key for key, route in mutating.items() if not _is_gated(route)}
    assert ungated == READ_ONLY_POSTS
