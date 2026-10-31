"""Rate limiting must survive a Redis outage.

Regression for a bug where an unreachable Redis made slowapi's middleware
hand the redis ``ConnectionError`` to its 429 handler, which reads
``exc.detail`` and crashed: every endpoint except ``/health`` returned 500.
The limiter now falls back to in-memory counters with the same limits.

Redis is pointed at a closed local port so these tests behave the same
whether or not a real Redis is running (CI starts one on 6379).
"""

from __future__ import annotations

import importlib
import socket
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from grimoire.api.rate_limit import DEFAULT_LIMIT

# `grimoire.config` exports a `settings` object that shadows the submodule.
settings_mod = importlib.import_module("grimoire.config.settings")


def _closed_port() -> int:
    """Return a local TCP port with nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def client_without_redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    base = settings_mod.get_settings()
    patched = base.model_copy(
        update={
            "redis": base.redis.model_copy(
                update={"host": "127.0.0.1", "port": _closed_port()}
            )
        }
    )
    monkeypatch.setattr(settings_mod, "get_settings", lambda: patched)

    from grimoire.api.dependencies import get_db_session
    from grimoire.api.main import create_app

    app = create_app(use_lifespan=False)

    # Auth looks the key up in the DB; make every lookup miss.
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result

    async def override_db() -> Iterator[AsyncMock]:
        yield session

    app.dependency_overrides[get_db_session] = override_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


class TestRedisOutage:
    def test_requests_are_served_not_500(
        self, client_without_redis: TestClient
    ) -> None:
        resp = client_without_redis.get("/api/v1/documents")
        assert resp.status_code == 401, resp.text

    def test_invalid_key_still_rejected(self, client_without_redis: TestClient) -> None:
        resp = client_without_redis.get(
            "/api/v1/documents", headers={"X-API-Key": "grim_agt_bogus"}
        )
        assert resp.status_code == 401, resp.text

    def test_limits_still_enforced_in_memory(
        self, client_without_redis: TestClient
    ) -> None:
        """Failing open would let a Redis outage disable brute-force limits."""
        limit = int(DEFAULT_LIMIT.split("/")[0])
        codes = [
            client_without_redis.get("/api/v1/documents").status_code
            for _ in range(limit + 1)
        ]
        assert codes[:limit] == [401] * limit
        assert codes[limit] == 429

    def test_rate_limit_response_is_well_formed(
        self, client_without_redis: TestClient
    ) -> None:
        limit = int(DEFAULT_LIMIT.split("/")[0])
        for _ in range(limit):
            client_without_redis.get("/api/v1/documents")
        resp = client_without_redis.get("/api/v1/documents")
        assert resp.status_code == 429
        assert "Rate limit exceeded" in resp.json()["error"]

    def test_health_unaffected(self, client_without_redis: TestClient) -> None:
        assert client_without_redis.get("/health").status_code == 200
