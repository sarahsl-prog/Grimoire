"""``require_min_tier``: the REST counterpart of the MCP server's tier gate."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from grimoire.api.auth import get_api_key, require_min_tier
from grimoire.db.models import ApiKey, ApiKeyTier

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _key(tier: ApiKeyTier) -> ApiKey:
    return ApiKey(
        id="k",
        name="my-key",
        tier=tier,
        key_prefix=f"grim_{tier.value}_tst",
        key_hash="x",
        created_at=NOW,
    )


def _client(tier: ApiKeyTier, minimum: ApiKeyTier) -> TestClient:
    app = FastAPI()

    @app.get("/gated")
    async def gated(key: ApiKey = Depends(require_min_tier(minimum))) -> dict[str, str]:
        return {"tier": key.tier.value}

    async def override() -> ApiKey:
        return _key(tier)

    app.dependency_overrides[get_api_key] = override
    return TestClient(app)


A, D, R = ApiKeyTier.AGENT, ApiKeyTier.DEV, ApiKeyTier.READ


@pytest.mark.parametrize(
    ("held", "minimum", "allowed"),
    [
        (A, A, True),
        (A, D, True),
        (A, R, True),
        (D, A, False),
        (D, D, True),
        (D, R, True),
        (R, A, False),
        (R, D, False),
        (R, R, True),
    ],
)
def test_tiers_are_hierarchical(
    held: ApiKeyTier, minimum: ApiKeyTier, allowed: bool
) -> None:
    response = _client(held, minimum).get("/gated")
    assert response.status_code == (200 if allowed else 403), response.text


def test_denial_names_the_required_and_held_tier_but_not_the_key() -> None:
    response = _client(R, A).get("/gated")

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "agt" in detail  # what is required
    assert "rdl" in detail  # what the caller has
    assert "grim_" not in detail  # never echo any part of the key
    assert "my-key" not in detail


def test_the_gate_is_built_on_get_api_key_so_unauthenticated_is_401() -> None:
    app = FastAPI()

    @app.get("/gated")
    async def gated(
        key: ApiKey = Depends(require_min_tier(ApiKeyTier.DEV)),
    ) -> dict[str, str]:
        return {}

    # No override: the real get_api_key runs and finds no X-API-Key header.
    # (It also needs a DB session, so supply an inert one.)
    from grimoire.api.dependencies import get_db_session

    async def no_db():  # type: ignore[no-untyped-def]
        yield None

    app.dependency_overrides[get_db_session] = no_db
    assert TestClient(app).get("/gated").status_code == 401
