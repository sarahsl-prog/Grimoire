"""The MCP server is rate limited like the REST API.

The dedicated MCP app (the `mcp` service, which Caddy routes /mcp/* to) had
no limiter at all, and its auth middleware applied neither the per-key tier
limit nor the cap on invalid keys.
"""

from __future__ import annotations

import importlib
import socket
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from grimoire.db.models import ApiKey, ApiKeyTier

settings_mod = importlib.import_module("grimoire.config.settings")
URL = "/mcp/messages/"
READ_KEY = "grim_rdl_mcp_secret"


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _ok_app(scope: Any, receive: Any, send: Any) -> None:
    """Stand-in for the MCP SSE app: anything that gets here is let through."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


@pytest.fixture
def mcp(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, AsyncMock]]:
    base = settings_mod.get_settings()
    patched = base.model_copy(
        update={
            "auth": base.auth.model_copy(
                update={
                    "rate_limits": {
                        "agt": "8/minute",
                        "dvl": "5/minute",
                        "rdl": "3/minute",
                    },
                    "failed_auth_limit": "3/minute",
                }
            ),
            "redis": base.redis.model_copy(
                update={"host": "127.0.0.1", "port": _closed_port()}
            ),
        }
    )
    monkeypatch.setattr(settings_mod, "get_settings", lambda: patched)

    key = ApiKey(
        id="k",
        name="k",
        tier=ApiKeyTier.READ,
        key_prefix="grim_rdl_mcp",
        key_hash="x",
        created_at=datetime.now(UTC),
    )

    async def _lookup(raw: str, db: Any) -> ApiKey | None:
        return key if raw == READ_KEY else None

    authenticate = AsyncMock(side_effect=_lookup)
    monkeypatch.setattr("grimoire.api.auth.authenticate_api_key", authenticate)

    @asynccontextmanager
    async def _session() -> Any:
        yield AsyncMock()

    manager = MagicMock()
    manager.session = _session

    with (
        patch("grimoire.mcp.router.get_mcp_app", return_value=_ok_app),
        patch("grimoire.db.session.get_db_manager", return_value=manager),
    ):
        from grimoire.mcp.app import create_mcp_app

        app = create_mcp_app(use_lifespan=False)
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, authenticate


def _codes(client: TestClient, n: int, key: str) -> list[int]:
    return [client.post(URL, headers={"X-API-Key": key}).status_code for _ in range(n)]


def test_mcp_app_has_a_limiter(mcp: tuple[TestClient, AsyncMock]) -> None:
    client, _ = mcp
    assert client.app.state.limiter is not None  # type: ignore[attr-defined]


def test_tier_limit_applies_to_mcp_calls(mcp: tuple[TestClient, AsyncMock]) -> None:
    client, _ = mcp
    assert _codes(client, 3, READ_KEY) == [200, 200, 200]
    resp = client.post(URL, headers={"X-API-Key": READ_KEY})
    assert resp.status_code == 429
    assert "rdl tier" in resp.json()["detail"]
    assert int(resp.headers["retry-after"]) >= 1


def test_invalid_keys_are_capped(mcp: tuple[TestClient, AsyncMock]) -> None:
    client, authenticate = mcp
    assert _codes(client, 4, "grim_rdl_guess") == [401, 401, 401, 429]
    assert authenticate.await_count == 3  # 4th refused before the lookup


def test_health_is_not_rate_limited(mcp: tuple[TestClient, AsyncMock]) -> None:
    client, _ = mcp
    assert {client.get("/health").status_code for _ in range(40)} == {200}
