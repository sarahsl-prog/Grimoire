"""Tests for the Grimoire MCP server.

Covers:
- Server creation and tool registration
- Tier-based access control (READ / DEV / AGENT)
- HTTP/SSE mount inside the FastAPI app
- Tool execution with mocked service layer
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from grimoire.api.main import create_app
from grimoire.db.models import ApiKey, ApiKeyTier
from grimoire.mcp.server import create_mcp_server
from grimoire.mcp.auth_stdio import set_current_api_key


def _make_api_key(tier: ApiKeyTier = ApiKeyTier.AGENT) -> ApiKey:
    """Create a mock ApiKey for testing."""
    return ApiKey(
        id="test-key-12345678",
        name="test-key",
        tier=tier,
        key_prefix="grim_agt_tst",
        key_hash="$2b$12$fakehash",
        created_at=datetime.now(timezone.utc),
    )


@asynccontextmanager
async def _fake_db_context(mock_db=None):
    """Async context manager that yields a mock DB session."""
    yield mock_db or AsyncMock()


@pytest.fixture
def mcp_server():
    """Create a test MCP server (stdio mode, lifespan disabled)."""
    return create_mcp_server()


@pytest.fixture
def app():
    """Create a test FastAPI app with MCP mounted."""
    test_app = create_app(use_lifespan=False)
    if hasattr(test_app.state, "limiter") and test_app.state.limiter:
        test_app.state.limiter.enabled = False
    return test_app


@pytest.fixture
def client(app):
    """Test client with mocked DB and auth dependencies."""
    mock_session = AsyncMock()

    async def override_db():
        yield mock_session

    from grimoire.api.auth import get_api_key
    from grimoire.api.dependencies import get_db_session

    test_key = _make_api_key(ApiKeyTier.AGENT)

    async def override_api_key():
        return test_key

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_api_key] = override_api_key

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Server creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_creates_all_tools(mcp_server: Any) -> None:
    """All expected tools are registered on the server."""
    tools = await mcp_server.list_tools()
    names = {t.name for t in tools}
    expected = {
        "grimoire_search",
        "grimoire_ask",
        "grimoire_get_document",
        "grimoire_list_documents",
        "grimoire_list_categories",
        "grimoire_watch_status",
        "grimoire_pg_query",
        "grimoire_status",
        "grimoire_ingest_file",
        "grimoire_ingest_directory",
        "grimoire_generate",
        "grimoire_create_category",
        "grimoire_watch_start",
        "grimoire_delete_document",
    }
    assert expected <= names, f"Missing tools: {expected - names}"


# ---------------------------------------------------------------------------
# Tier-based access control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_tools_available_to_read_tier(mcp_server: Any) -> None:
    """READ-tier key can call search, ask, get, list, status, query."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent:
        mock_agent.return_value.search = AsyncMock(return_value=MagicMock(
            query="test", results=[], total_results=0, duration_ms=1,
        ))
        with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_db_context):
            result = await mcp_server.call_tool("grimoire_search", {"params": {"query": "test"}})
        assert result.content[0].text.startswith('{"status": "ok"')


@pytest.mark.asyncio
async def test_ingest_requires_dev_tier(mcp_server: Any) -> None:
    """READ-tier key cannot call ingest_file."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with pytest.raises(ToolError) as exc_info:
        await mcp_server.call_tool("grimoire_ingest_file", {
            "params": {"file_path": "/tmp/test.txt"},
        })
    text = str(exc_info.value)
    assert "requires API key tier" in text
    assert "rdl" in text


@pytest.mark.asyncio
async def test_ingest_allowed_for_dev_tier(mcp_server: Any) -> None:
    """DEV-tier key can call ingest_file."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    with patch("grimoire.mcp.tools.get_ingestion_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_db_context):
        mock_agent.return_value.ingest_file = AsyncMock(return_value=MagicMock(
            model_dump=lambda: {"file_path": "/tmp/test.txt", "status": "completed"},
        ))
        result = await mcp_server.call_tool("grimoire_ingest_file", {
            "params": {"file_path": "/tmp/test.txt"},
        })
        assert result.content[0].text.startswith('{"status": "ok"')


@pytest.mark.asyncio
async def test_delete_requires_agent_tier(mcp_server: Any) -> None:
    """DEV-tier key cannot call delete_document."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    with pytest.raises(ToolError) as exc_info:
        await mcp_server.call_tool("grimoire_delete_document", {
            "params": {"document_id": "doc-123"},
        })
    text = str(exc_info.value)
    assert "requires API key tier" in text
    assert "dvl" in text


@pytest.mark.asyncio
async def test_delete_allowed_for_agent_tier(mcp_server: Any) -> None:
    """AGENT-tier key can call delete_document."""
    set_current_api_key(_make_api_key(ApiKeyTier.AGENT))

    mock_db = AsyncMock()
    mock_doc = MagicMock()
    mock_doc.id = "doc-123"
    mock_doc.chunks = []
    mock_doc.tags = []
    mock_doc.title = "Test"
    mock_doc.source_path = "/tmp/test.txt"
    mock_doc.file_type.value = "txt"
    mock_doc.storage_backend.value = "local"
    mock_doc.processing_status.value = "completed"
    mock_doc.size_bytes = 100
    mock_doc.created_at = datetime.now(timezone.utc)
    mock_doc.updated_at = datetime.now(timezone.utc)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_doc
    mock_db.execute.return_value = mock_result

    @asynccontextmanager
    async def _ctx():
        yield mock_db

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _ctx):
        result = await mcp_server.call_tool("grimoire_delete_document", {
            "params": {"document_id": "doc-123"},
        })
    assert result.content[0].text.startswith('{"status": "ok"')


# ---------------------------------------------------------------------------
# HTTP/SSE mount
# ---------------------------------------------------------------------------


def test_mcp_route_present_in_app(client: TestClient) -> None:
    """The /mcp route is mounted in the FastAPI app."""
    from starlette.routing import Mount
    paths = [r.path for r in client.app.routes if isinstance(r, Mount)]
    assert "/mcp" in paths


def test_mcp_requires_api_key(client: TestClient) -> None:
    """Requests to /mcp without X-API-Key return 401."""
    response = client.get("/mcp/sse")
    assert response.status_code == 401


def test_mcp_accepts_valid_api_key(client: TestClient) -> None:
    """Requests to /mcp with a valid X-API-Key pass auth."""
    response = client.get("/mcp/sse", headers={"X-API-Key": "grim_agt_testkey123"})
    assert response.status_code != 401
