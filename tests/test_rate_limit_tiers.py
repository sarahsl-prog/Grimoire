"""Two-stage rate limiting: per-IP before auth, per-key tier limit after.

Regression for the limiter keying every request by client IP at 30/minute:
slowapi's middleware runs before authentication, so ``request.state.api_key``
was never set when it chose the bucket, and tier limits (agent 600/minute)
were never applied. Also covers the per-IP cap on invalid API keys.

These go through the real ``get_api_key`` dependency (only the database
lookup is patched) with Redis pointed at a closed port, so the limiter uses
its in-memory fallback and each test starts from empty counters.
"""

from __future__ import annotations

import importlib
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from grimoire.db.models import ApiKey, ApiKeyTier

settings_mod = importlib.import_module("grimoire.config.settings")

SMALL_LIMITS = {"agt": "8/minute", "dvl": "5/minute", "rdl": "3/minute"}
URL = "/api/v1/documents"


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _key(tier: ApiKeyTier, prefix: str) -> ApiKey:
    return ApiKey(
        id=prefix,
        name=prefix,
        tier=tier,
        key_prefix=prefix,
        key_hash="x",
        created_at=datetime.now(UTC),
    )


KEYS = {
    "grim_rdl_one_secret": _key(ApiKeyTier.READ, "grim_rdl_one"),
    "grim_rdl_two_secret": _key(ApiKeyTier.READ, "grim_rdl_two"),
    "grim_agt_one_secret": _key(ApiKeyTier.AGENT, "grim_agt_one"),
}


def _client(
    monkeypatch: pytest.MonkeyPatch,
    rate_limits: dict[str, str] | None = None,
    failed_auth_limit: str = "10/minute",
) -> tuple[TestClient, AsyncMock]:
    base = settings_mod.get_settings()
    auth = base.auth.model_copy(
        update={
            "rate_limits": rate_limits or base.auth.rate_limits,
            "failed_auth_limit": failed_auth_limit,
        }
    )
    redis = base.redis.model_copy(update={"host": "127.0.0.1", "port": _closed_port()})
    patched = base.model_copy(update={"auth": auth, "redis": redis})
    monkeypatch.setattr(settings_mod, "get_settings", lambda: patched)

    async def _lookup(raw: str, db: Any) -> ApiKey | None:
        return KEYS.get(raw)

    authenticate = AsyncMock(side_effect=_lookup)
    monkeypatch.setattr("grimoire.api.auth.authenticate_api_key", authenticate)

    from grimoire.api.dependencies import get_db_session
    from grimoire.api.main import create_app

    app = create_app(use_lifespan=False)
    session = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = 0
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)

    async def _db() -> Any:
        yield session

    app.dependency_overrides[get_db_session] = _db
    return TestClient(app, raise_server_exceptions=False), authenticate


def _codes(client: TestClient, n: int, key: str | None) -> list[int]:
    headers = {"X-API-Key": key} if key else {}
    return [client.get(URL, headers=headers).status_code for _ in range(n)]


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    client, _ = _client(monkeypatch, SMALL_LIMITS)
    with client:
        yield client


class TestTierLimits:
    def test_read_key_is_limited_at_its_tier_limit(self, small: TestClient) -> None:
        codes = _codes(small, 4, "grim_rdl_one_secret")
        assert 429 not in codes[:3]
        assert codes[3] == 429

    def test_429_names_the_tier_and_sets_retry_after(self, small: TestClient) -> None:
        _codes(small, 3, "grim_rdl_one_secret")
        resp = small.get(URL, headers={"X-API-Key": "grim_rdl_one_secret"})
        assert resp.status_code == 429
        assert "rdl tier" in resp.json()["detail"]
        assert int(resp.headers["Retry-After"]) >= 1

    def test_each_key_has_its_own_bucket(self, small: TestClient) -> None:
        _codes(small, 3, "grim_rdl_one_secret")
        assert 429 not in _codes(small, 3, "grim_rdl_two_secret")

    def test_higher_tier_gets_its_higher_limit(self, small: TestClient) -> None:
        codes = _codes(small, 9, "grim_agt_one_secret")
        assert 429 not in codes[:8]
        assert codes[8] == 429

    def test_agent_key_is_not_capped_at_the_anonymous_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: every request shared one 30/minute per-IP bucket."""
        client, _ = _client(monkeypatch)  # default limits: agt 600/minute
        with client:
            assert 429 not in _codes(client, 40, "grim_agt_one_secret")

    def test_anonymous_requests_keep_the_default_limit(self, small: TestClient) -> None:
        codes = _codes(small, 31, None)
        assert 429 not in codes[:30]
        assert codes[30] == 429


class TestFailedAuthLimit:
    def test_invalid_keys_are_capped_per_ip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, authenticate = _client(monkeypatch, failed_auth_limit="3/minute")
        with client:
            codes = _codes(client, 4, "grim_rdl_guess")
            assert codes == [401, 401, 401, 429]
            # The 4th was refused before the key lookup (no DB, no bcrypt).
            assert authenticate.await_count == 3

    def test_valid_keys_do_not_count_as_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = _client(monkeypatch, failed_auth_limit="2/minute")
        with client:
            _codes(client, 5, "grim_agt_one_secret")
            assert _codes(client, 2, "grim_rdl_guess") == [401, 401]


def test_preauth_ceiling_is_the_highest_tier_limit() -> None:
    from grimoire.api.rate_limit import _highest_limit

    assert _highest_limit(SMALL_LIMITS) == "8/minute"
    # Compared per second, not by raw count.
    assert _highest_limit({"agt": "100/hour", "rdl": "5/second"}) == "5/second"


def test_stages_use_memory_counters_when_storage_is_down() -> None:
    """Regression: the per-key and invalid-key stages failed open on a Redis
    outage whenever slowapi's own middleware hadn't already switched to its
    in-memory fallback (it never runs for the mounted /mcp app)."""
    from fastapi import HTTPException
    from limits.storage import MemoryStorage
    from limits.strategies import FixedWindowRateLimiter

    from grimoire.api.rate_limit import check_failed_auth, record_failed_auth

    broken = MagicMock()
    broken.hit.side_effect = ConnectionError("redis down")
    broken.test.side_effect = ConnectionError("redis down")
    limiter = MagicMock(enabled=True, limiter=broken)
    limiter._fallback_limiter = FixedWindowRateLimiter(MemoryStorage())
    request = MagicMock()
    request.app.state.limiter = limiter
    request.client.host = "203.0.113.9"

    with patch(
        "grimoire.api.rate_limit._auth_config",
        return_value=MagicMock(failed_auth_limit="2/minute"),
    ):
        for _ in range(2):
            check_failed_auth(request)
            record_failed_auth(request)
        with pytest.raises(HTTPException) as exc:
            check_failed_auth(request)
    assert exc.value.status_code == 429
