"""Tests for the Grimoire FastAPI REST API.

Tests cover:
- Health check endpoint
- Ingest endpoints (file, directory)
- Query endpoints (ask, search)
- Document endpoints (list, detail, delete)
- Category endpoints (list, create, delete)
- Generate endpoint
- Watch endpoints (start, stop, status)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from grimoire.api.main import create_app
from grimoire.db.models import ApiKey, ApiKeyTier

_DEPS = "grimoire.api.dependencies"
_ROUTES_INGEST = "grimoire.api.routes.ingest"
_ROUTES_QUERY = "grimoire.api.routes.query"
_ROUTES_GENERATE = "grimoire.api.routes.generate"
_ROUTES_DOCUMENTS = "grimoire.api.routes.documents"
_ROUTES_CATEGORIES = "grimoire.api.routes.categories"
_ROUTES_WATCH = "grimoire.api.routes.watch"


def _make_test_api_key() -> ApiKey:
    """Create a mock ApiKey for testing."""
    key = ApiKey(
        id="test-key-12345678",
        name="test-key",
        tier=ApiKeyTier.AGENT,
        key_prefix="grim_agt_tst",
        key_hash="$2b$12$fakehash",
        created_at=datetime.now(timezone.utc),
    )
    return key


@pytest.fixture
def app():
    """Create a test FastAPI application (skip lifespan DB init)."""
    test_app = create_app(use_lifespan=False)
    return test_app


@pytest.fixture
def client(app):
    """Test client with mocked DB and auth dependencies."""
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


@pytest.fixture
def mock_db_session():
    """A mock async DB session."""
    return AsyncMock()


# =============================================================================
# Health Check
# =============================================================================


class TestHealthCheck:
    def test_health_check(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# =============================================================================
# Ingest
# =============================================================================


class TestIngestAPI:
    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_ingest_file(self, mock_get_agent, client):
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "file_path": "/tmp/test.pdf",
            "document_id": "doc-1",
            "status": "completed",
            "chunks_created": 5,
            "vectors_stored": 5,
            "tags_applied": 2,
            "error_message": None,
            "duration_ms": 100,
        }
        mock_agent.ingest_file = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        resp = client.post("/api/v1/ingest/file", json={"file_path": "/tmp/test.pdf"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["document_id"] == "doc-1"
        assert data["chunks_created"] == 5

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_ingest_directory(self, mock_get_agent, client):
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "total": 3,
            "succeeded": 2,
            "skipped": 1,
            "failed": 0,
            "results": [],
            "duration_ms": 500,
        }
        mock_agent.ingest_directory = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        resp = client.post("/api/v1/ingest/directory", json={"directory": "/tmp/docs"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["succeeded"] == 2


# =============================================================================
# Query
# =============================================================================


class TestQueryAPI:
    @patch(f"{_ROUTES_QUERY}.get_query_agent")
    def test_ask_question(self, mock_get_agent, client):
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "query": "What is Grimoire?",
            "answer": "A knowledge management tool.",
            "citations": [],
            "model_used": "test-model",
            "search_results_count": 3,
            "cached": False,
            "duration_ms": 200,
        }
        mock_agent.query = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        resp = client.post("/api/v1/query/ask", json={"query": "What is Grimoire?"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "A knowledge management tool."
        assert data["cached"] is False

    @patch(f"{_ROUTES_QUERY}.get_query_agent")
    def test_search_documents(self, mock_get_agent, client):
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.query = "test query"
        mock_result.results = [
            {"chunk_id": "c1", "document_id": "d1", "content": "test", "score": 0.9}
        ]
        mock_result.total_results = 1
        mock_result.duration_ms = 50
        mock_agent.search = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        resp = client.post("/api/v1/query/search", json={"query": "test query"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_results"] == 1
        assert len(data["results"]) == 1


# =============================================================================
# Documents
# =============================================================================


class TestDocumentsAPI:
    def test_list_documents(self, app):
        """Test listing documents with mocked DB."""
        mock_session = AsyncMock()

        # Mock the execute calls for both data query and count query
        mock_doc = MagicMock()
        mock_doc.id = "doc-1"
        mock_doc.title = "Test Doc"
        mock_doc.source_path = "/tmp/test.pdf"
        mock_doc.file_type = "pdf"
        mock_doc.storage_backend = "local"
        mock_doc.processing_status = "completed"
        mock_doc.size_bytes = 1024
        mock_doc.created_at = None
        mock_doc.updated_at = None

        # data query result
        data_result = MagicMock()
        data_result.scalars.return_value.all.return_value = [mock_doc]

        # count query result
        count_result = MagicMock()
        count_result.scalar.return_value = 1

        mock_session.execute = AsyncMock(side_effect=[count_result, data_result])

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
            resp = c.get("/api/v1/documents")

        app.dependency_overrides.clear()
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["documents"]) == 1
        assert data["documents"][0]["id"] == "doc-1"

    def test_get_document_not_found(self, app):
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)

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
            resp = c.get("/api/v1/documents/nonexistent")

        app.dependency_overrides.clear()
        assert resp.status_code == 404

    def test_delete_document_not_found(self, app):
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)

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
            resp = c.delete("/api/v1/documents/nonexistent")

        app.dependency_overrides.clear()
        assert resp.status_code == 404


# =============================================================================
# Categories
# =============================================================================


class TestCategoriesAPI:
    def test_list_categories(self, app):
        mock_session = AsyncMock()

        cat_result = MagicMock()
        cat_result.scalars.return_value.all.return_value = []

        count_result = MagicMock()
        count_result.scalar.return_value = 0

        mock_session.execute = AsyncMock(side_effect=[cat_result, count_result])

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
            resp = c.get("/api/v1/categories")

        app.dependency_overrides.clear()
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["categories"] == []

    def test_delete_category_not_found(self, app):
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)

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
            resp = c.delete("/api/v1/categories/nonexistent")

        app.dependency_overrides.clear()
        assert resp.status_code == 404


# =============================================================================
# Generate
# =============================================================================


class TestGenerateAPI:
    @patch(f"{_ROUTES_GENERATE}.get_content_gen_agent")
    def test_generate_summary(self, mock_get_agent, client):
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "content": "This is a summary.",
            "content_type": "summary",
            "document_ids": ["doc-1"],
            "model_used": "test-model",
            "cached": False,
            "generation_id": "gen-1",
            "duration_ms": 300,
        }
        mock_agent.generate_summary = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        resp = client.post(
            "/api/v1/generate",
            json={"document_ids": ["doc-1"], "content_type": "summary"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "This is a summary."
        assert data["content_type"] == "summary"

    @patch(f"{_ROUTES_GENERATE}.get_content_gen_agent")
    def test_generate_extract_missing_query(self, mock_get_agent, client):
        mock_get_agent.return_value = MagicMock()

        resp = client.post(
            "/api/v1/generate",
            json={"document_ids": ["doc-1"], "content_type": "extract"},
        )
        assert resp.status_code == 400
        assert "query" in resp.json()["detail"].lower()

    def test_generate_unsupported_type(self, client):
        resp = client.post(
            "/api/v1/generate",
            json={"document_ids": ["doc-1"], "content_type": "invalid_type"},
        )
        # Should get a validation or 400 error
        assert resp.status_code in (400, 422, 500)


# =============================================================================
# Watch
# =============================================================================


class TestWatchAPI:
    def test_watch_not_initialized(self, client):
        """Watch endpoints should return 503 when watcher not set."""
        resp = client.get("/api/v1/watch/status")
        assert resp.status_code == 503
        assert "not initialized" in resp.json()["detail"].lower()

    def test_start_watch_not_initialized(self, client):
        resp = client.post(
            "/api/v1/watch/start",
            json={"path": "/tmp/docs"},
        )
        assert resp.status_code == 503

    @patch(f"{_ROUTES_WATCH}._watcher")
    def test_stop_watch_not_found(self, mock_watcher, client):
        mock_watcher_inst = MagicMock()
        mock_watcher_inst.unwatch = AsyncMock(return_value=False)

        with patch(f"{_ROUTES_WATCH}._get_watcher", return_value=mock_watcher_inst):
            resp = client.delete("/api/v1/watch/nonexistent")
        assert resp.status_code == 404


# =============================================================================
# Edge Cases
# =============================================================================


class TestAPIEdgeCases:
    def test_unknown_route(self, client):
        resp = client.get("/api/v1/nonexistent")
        assert resp.status_code == 404

    def test_invalid_json_body(self, client):
        resp = client.post(
            "/api/v1/ingest/file",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    def test_missing_required_field(self, client):
        resp = client.post("/api/v1/ingest/file", json={})
        assert resp.status_code == 422

    def test_query_missing_query_field(self, client):
        resp = client.post("/api/v1/query/ask", json={})
        assert resp.status_code == 422
