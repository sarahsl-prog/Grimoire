"""Security-focused API tests: path traversal, auth edge cases, and rate limits.

These tests fill the gaps identified in the code-review report (May 26):
- Path traversal (C-2)
- API key auth edge cases
- Rate limiting on /health (W-7)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from grimoire.api.auth import authenticate_api_key, generate_api_key
from grimoire.api.main import create_app
from grimoire.api.routes.ingest import _is_path_allowed
from grimoire.db.models import ApiKey, ApiKeyTier

_ROUTES_INGEST = "grimoire.api.routes.ingest"


def _make_test_api_key(**overrides: Any) -> ApiKey:
    """Create a mock ApiKey with sensible defaults."""
    defaults = {
        "id": "test-key-12345678",
        "name": "test-key",
        "tier": ApiKeyTier.AGENT,
        "key_prefix": "grim_agt_tst",
        "key_hash": "$2b$12$fakehash",
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return ApiKey(**defaults)


# =============================================================================
# Path Traversal
# =============================================================================


class TestPathTraversalDirect:
    """Unit tests for the ``_is_path_allowed`` sanitization helper."""

    def test_null_bytes_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _is_path_allowed("/tmp/foo\x00bar")
        assert exc_info.value.status_code == 400

    def test_long_path_rejected(self) -> None:
        long_path = "/tmp/" + "a" * 3000
        with pytest.raises(HTTPException) as exc_info:
            _is_path_allowed(long_path)
        assert exc_info.value.status_code == 400
        assert "2048" in exc_info.value.detail

    def test_root_escape_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _is_path_allowed("/etc/passwd")
        assert exc_info.value.status_code == 403

    def test_dotdot_escape_rejected(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _is_path_allowed("/tmp/../../etc/passwd")
        assert exc_info.value.status_code == 403

    def test_symlink_escape_rejected(self, tmp_path) -> None:
        """A symlink inside /tmp that points outside should be blocked."""
        symlink = tmp_path / "evil_link"
        symlink.symlink_to("/etc/passwd")
        with pytest.raises(HTTPException) as exc_info:
            _is_path_allowed(str(symlink))
        assert exc_info.value.status_code == 403
        symlink.unlink()

    def test_allowed_tmp_path(self, tmp_path) -> None:
        """A real file under /tmp should be accepted."""
        real_file = tmp_path / "safe.txt"
        real_file.write_text("hello")
        result = _is_path_allowed(str(real_file))
        assert result.is_relative_to("/tmp")
        real_file.unlink()


class TestPathTraversalEndpoints:
    """Integration tests via the FastAPI ingest endpoints."""

    @pytest.fixture
    def client(self):
        app = create_app(use_lifespan=False)
        mock_session = AsyncMock()

        async def override_db():
            yield mock_session

        from grimoire.api.auth import get_api_key
        from grimoire.api.dependencies import get_db_session

        test_key = _make_test_api_key()

        async def override_api_key():
            return test_key

        app.dependency_overrides[get_db_session] = override_db
        app.dependency_overrides[get_api_key] = override_api_key
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
        app.dependency_overrides.clear()

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_ingest_file_path_traversal(self, mock_get_agent, client) -> None:
        """Requesting a file outside allowed roots should yield 403."""
        resp = client.post("/api/v1/ingest/file", json={"file_path": "/etc/passwd"})
        assert resp.status_code == 403

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_ingest_file_null_bytes(self, mock_get_agent, client) -> None:
        resp = client.post(
            "/api/v1/ingest/file",
            json={"file_path": "/tmp/foo\x00bar"},
        )
        assert resp.status_code == 400

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_ingest_file_too_long(self, mock_get_agent, client) -> None:
        resp = client.post(
            "/api/v1/ingest/file",
            json={"file_path": "/tmp/" + "a" * 3000},
        )
        assert resp.status_code == 400

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_ingest_directory_path_traversal(self, mock_get_agent, client) -> None:
        resp = client.post(
            "/api/v1/ingest/directory",
            json={"directory": "/var/log"},
        )
        assert resp.status_code == 403


# =============================================================================
# API Key Auth
# =============================================================================


class TestAuthEdgeCases:
    """Edge case coverage for ``authenticate_api_key``."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    # --- Direct helper tests ---

    @pytest.mark.asyncio
    async def test_empty_key(self, mock_db) -> None:
        assert await authenticate_api_key("", mock_db) is None

    @pytest.mark.asyncio
    async def test_wrong_prefix(self, mock_db) -> None:
        assert await authenticate_api_key("notgrim_abc123", mock_db) is None

    @pytest.mark.asyncio
    async def test_no_prefix_at_all(self, mock_db) -> None:
        assert await authenticate_api_key("totally_legit_key", mock_db) is None

    @pytest.mark.asyncio
    async def test_no_matching_prefix_in_db(self, mock_db) -> None:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        assert await authenticate_api_key("grim_agt_test12345678901", mock_db) is None

    @pytest.mark.asyncio
    async def test_expired_key(self, mock_db) -> None:
        key = _make_test_api_key(
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = key
        mock_db.execute.return_value = mock_result
        assert await authenticate_api_key("grim_agt_tst1234567890123456789012", mock_db) is None

    @pytest.mark.asyncio
    async def test_revoked_key_filtered_by_query(self, mock_db) -> None:
        """A revoked key should be excluded by the SQL query (revoked_at IS NULL)."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # DB returns nothing
        mock_db.execute.return_value = mock_result
        assert await authenticate_api_key("grim_agt_tst1234567890123456789012", mock_db) is None

    @pytest.mark.asyncio
    async def test_bcrypt_hash_mismatch(self, mock_db) -> None:
        import bcrypt

        real_key = "grim_agt_tst1234567890123456789012"
        wrong_hash = bcrypt.hashpw(b"wrong_key", bcrypt.gensalt()).decode()
        key = _make_test_api_key(key_hash=wrong_hash)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = key
        mock_db.execute.return_value = mock_result
        assert await authenticate_api_key(real_key, mock_db) is None

    @pytest.mark.asyncio
    async def test_valid_key(self, mock_db) -> None:
        import bcrypt

        real_key = "grim_agt_tst1234567890123456789012"
        key_hash = bcrypt.hashpw(real_key.encode(), bcrypt.gensalt()).decode()
        key = _make_test_api_key(key_hash=key_hash)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = key
        mock_db.execute.return_value = mock_result

        result = await authenticate_api_key(real_key, mock_db)
        assert result is key
        # last_used_at should have been updated
        assert key.last_used_at is not None
        assert mock_db.flush.called

    @pytest.mark.asyncio
    async def test_flush_failure_isolated(self, mock_db) -> None:
        """If db.flush() fails, auth should still succeed (fire-and-forget)."""
        import bcrypt

        real_key = "grim_agt_tst1234567890123456789012"
        key_hash = bcrypt.hashpw(real_key.encode(), bcrypt.gensalt()).decode()
        key = _make_test_api_key(key_hash=key_hash)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = key
        mock_db.execute.return_value = mock_result
        mock_db.flush.side_effect = Exception("DB connection lost")

        result = await authenticate_api_key(real_key, mock_db)
        assert result is key  # auth succeeded despite flush failure


class TestAuthDependency:
    """Auth via the FastAPI dependency (endpoint level)."""

    def test_missing_api_key(self) -> None:
        app = create_app(use_lifespan=False)
        mock_session = AsyncMock()

        # configure execute so scalar_one_or_none returns None (no matching key)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        async def override_db():
            yield mock_session

        from grimoire.api.dependencies import get_db_session
        app.dependency_overrides[get_db_session] = override_db

        with TestClient(app) as c:
            # /health is public and should 200
            resp = c.get("/health")
            assert resp.status_code == 200

            # Protected endpoint → 401 because no X-API-Key header
            resp = c.get("/api/v1/documents")
            assert resp.status_code == 401

        app.dependency_overrides.clear()

    def test_invalid_api_key(self) -> None:
        app = create_app(use_lifespan=False)
        mock_session = AsyncMock()

        # configure execute so scalar_one_or_none returns None (no matching key)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        async def override_db():
            yield mock_session

        from grimoire.api.dependencies import get_db_session
        app.dependency_overrides[get_db_session] = override_db

        with TestClient(app) as c:
            resp = c.get(
                "/api/v1/documents",
                headers={"X-API-Key": "grim_agt_bogus"},
            )
            assert resp.status_code == 401

        app.dependency_overrides.clear()


# =============================================================================
# Rate Limiting
# =============================================================================


class TestRateLimiting:
    """Rate limit enforcement on public and protected endpoints."""

    def test_health_within_limit(self) -> None:
        """/health should serve normally with 60/minute burst headroom."""
        app = create_app(use_lifespan=False)
        with TestClient(app) as c:
            for _ in range(10):
                resp = c.get("/health")
                assert resp.status_code == 200
                assert resp.json() == {"status": "ok"}

    def test_health_rate_limit_eventually_tripped(self) -> None:
        """Hammering /health should eventually hit the 60/minute wall."""
        app = create_app(use_lifespan=False)
        with TestClient(app) as c:
            triggered = False
            for _ in range(65):
                resp = c.get("/health")
                if resp.status_code == 429:
                    triggered = True
                    break
            assert triggered, "Expected at least one 429 after exhausting the limit"

    def test_generate_api_key_produces_correct_prefix(self) -> None:
        raw, prefix, key_hash = generate_api_key(ApiKeyTier.AGENT)
        assert raw.startswith("grim_agt_")
        assert len(prefix) == 12
        assert prefix == raw[:12]
        assert key_hash.startswith("$2")
