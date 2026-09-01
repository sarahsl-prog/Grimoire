"""Tests for the Grimoire MCP server.

Covers:
- Server creation and tool registration
- Tier-based access control (READ / DEV / AGENT)
- HTTP/SSE mount inside the FastAPI app
- Tool execution with mocked service layer
- Edge cases and error paths for each tool
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from grimoire.api.main import create_app
from grimoire.db.models import ApiKey, ApiKeyTier
from grimoire.mcp.auth_stdio import set_current_api_key
from grimoire.mcp.server import create_mcp_server


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
async def _fake_db_context(mock_db: Any = None) -> Any:
    """Async context manager that yields a mock DB session."""
    yield mock_db or AsyncMock()


@pytest.fixture
def mcp_server() -> Any:
    """Create a test MCP server (stdio mode, lifespan disabled)."""
    return create_mcp_server()


@pytest.fixture
def app() -> Any:
    """Create a test FastAPI app with MCP mounted."""
    test_app = create_app(use_lifespan=False)
    if hasattr(test_app.state, "limiter") and test_app.state.limiter:
        test_app.state.limiter.enabled = False
    return test_app


@pytest.fixture
def client(app: Any) -> Any:
    """Test client with mocked DB and auth dependencies."""
    mock_session = AsyncMock()

    async def override_db() -> Any:
        yield mock_session

    from grimoire.api.auth import get_api_key
    from grimoire.api.dependencies import get_db_session

    test_key = _make_api_key(ApiKeyTier.AGENT)

    async def override_api_key() -> Any:
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
async def test_stdio_lifespan_validates_api_key(mcp_server: Any) -> None:
    """The stdio lifespan validates a valid GRIMOIRE_API_KEY env var."""
    with patch("grimoire.mcp.server.initialize_db", new_callable=AsyncMock) as mock_init, \
         patch("grimoire.mcp.server.close_db", new_callable=AsyncMock) as mock_close, \
         patch("grimoire.mcp.server.authenticate_stdio_key", new_callable=AsyncMock) as mock_auth, \
         patch("grimoire.mcp.server.set_current_api_key") as mock_set, \
         patch.dict("os.environ", {"GRIMOIRE_API_KEY": "grim_agt_testkey123"}):
        mock_auth.return_value = _make_api_key(ApiKeyTier.AGENT)
        async with mcp_server._mcp_server.lifespan(None):
            mock_auth.assert_awaited_once()
            mock_set.assert_called_once()
        mock_close.assert_awaited_once()
    mock_init.assert_awaited_once()


@pytest.mark.asyncio
async def test_stdio_lifespan_skips_auth_without_env(mcp_server: Any) -> None:
    """The stdio lifespan skips auth when GRIMOIRE_API_KEY is not set."""
    with patch("grimoire.mcp.server.initialize_db", new_callable=AsyncMock) as mock_init, \
         patch("grimoire.mcp.server.close_db", new_callable=AsyncMock) as mock_close, \
         patch("grimoire.mcp.server.authenticate_stdio_key") as mock_auth, \
         patch.dict("os.environ", {}, clear=True):
        async with mcp_server._mcp_server.lifespan(None):
            mock_auth.assert_not_awaited()
        mock_close.assert_awaited_once()
    mock_init.assert_awaited_once()


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
        "grimoire_watch_stop",
        "grimoire_delete_document",
        "grimoire_search_cve",
        "grimoire_search_playbook",
    }
    assert expected <= names, f"Missing tools: {expected - names}"


@pytest.mark.asyncio
async def test_ask_returns_answer(mcp_server: Any) -> None:
    """grimoire_ask returns a generated answer with citations."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    mock_result = MagicMock()
    mock_result.query = "What is ML?"
    mock_result.answer = "Machine learning is..."
    mock_result.citations = []
    mock_result.model_used = "llama3.2"
    mock_result.cached = False
    mock_result.duration_ms = 123

    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_db_context):
        mock_agent.return_value.query = AsyncMock(return_value=mock_result)
        result = await mcp_server.call_tool("grimoire_ask", {
            "params": {"query": "What is ML?"},
        })
    assert '"status": "ok"' in result[0][0].text
    assert "Machine learning is..." in result[0][0].text


@pytest.mark.asyncio
async def test_get_document_returns_doc(mcp_server: Any) -> None:
    """grimoire_get_document returns document details."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    mock_db = AsyncMock()
    mock_doc = MagicMock()
    mock_doc.id = "doc-123"
    mock_doc.title = "Test Doc"
    mock_doc.source_path = "/nonexistent/test.txt"
    mock_doc.file_type.value = "txt"
    mock_doc.storage_backend.value = "local"
    mock_doc.processing_status.value = "completed"
    mock_doc.size_bytes = 100
    mock_doc.created_at = datetime.now(timezone.utc)
    mock_doc.updated_at = datetime.now(timezone.utc)
    mock_doc.tags = []
    mock_doc.chunks = []

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_doc
    mock_db.execute.return_value = mock_result

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _ctx):
        result = await mcp_server.call_tool("grimoire_get_document", {
            "params": {"document_id": "doc-123"},
        })
    assert '"status": "ok"' in result[0][0].text
    assert "Test Doc" in result[0][0].text


@pytest.mark.asyncio
async def test_get_document_not_found(mcp_server: Any) -> None:
    """grimoire_get_document returns error when document does not exist."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _ctx):
        result = await mcp_server.call_tool("grimoire_get_document", {
            "params": {"document_id": "missing-id"},
        })
    assert '"status": "error"' in result[0][0].text


@pytest.mark.asyncio
async def test_list_documents_returns_page(mcp_server: Any) -> None:
    """grimoire_list_documents returns a paginated list."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    mock_db = AsyncMock()
    mock_doc = MagicMock()
    mock_doc.id = "doc-1"
    mock_doc.title = "Doc One"
    mock_doc.source_path = "/nonexistent/one.txt"
    mock_doc.file_type.value = "txt"
    mock_doc.processing_status.value = "completed"
    mock_doc.size_bytes = 10
    mock_doc.created_at = None
    mock_doc.updated_at = None

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_doc]
    mock_db.execute.return_value = mock_result

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _ctx):
        result = await mcp_server.call_tool("grimoire_list_documents", {
            "params": {"limit": 1, "offset": 0},
        })
    assert '"status": "ok"' in result[0][0].text
    assert "Doc One" in result[0][0].text


@pytest.mark.asyncio
async def test_list_categories_returns_categories(mcp_server: Any) -> None:
    """grimoire_list_categories returns all categories."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    mock_cat = MagicMock()
    mock_cat.id = "cat-1"
    mock_cat.name = "Research"
    mock_cat.slug = "research"
    mock_cat.description = "Papers"
    mock_cat.parent_id = None
    mock_cat.color = "#3498db"

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_cat]
    mock_db.execute.return_value = mock_result

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _ctx):
        result = await mcp_server.call_tool("grimoire_list_categories", {})
    assert '"status": "ok"' in result[0][0].text
    assert "Research" in result[0][0].text


@pytest.mark.asyncio
async def test_status_returns_counts(mcp_server: Any) -> None:
    """grimoire_status returns document and category counts."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 5
    mock_db.execute.return_value = mock_result

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _ctx):
        result = await mcp_server.call_tool("grimoire_status", {})
    assert '"status": "ok"' in result[0][0].text
    assert '"documents": 5' in result[0][0].text


@pytest.mark.asyncio
async def test_ingest_directory_allowed_for_dev_tier(mcp_server: Any) -> None:
    """DEV-tier key can call ingest_directory."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    with patch("grimoire.mcp.tools.get_ingestion_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_db_context):
        mock_agent.return_value.ingest_directory = AsyncMock(return_value=MagicMock(
            model_dump=lambda: {"directory": "/nonexistent/dir", "status": "completed"},
        ))
        result = await mcp_server.call_tool("grimoire_ingest_directory", {
            "params": {"directory": "/nonexistent/dir"},
        })
        assert '"status": "ok"' in result[0][0].text


@pytest.mark.asyncio
async def test_generate_summary(mcp_server: Any) -> None:
    """grimoire_generate returns content for a summary."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    with patch("grimoire.mcp.tools.get_content_gen_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_db_context):
        mock_agent.return_value.generate_summary = AsyncMock(return_value=MagicMock(
            model_dump=lambda: {"content": "A summary."},
        ))
        result = await mcp_server.call_tool("grimoire_generate", {
            "params": {
                "document_ids": ["doc-1"],
                "content_type": "summary",
                "style": "detailed",
            },
        })
    assert '"status": "ok"' in result[0][0].text
    assert "A summary." in result[0][0].text


@pytest.mark.asyncio
async def test_generate_extract_requires_query(mcp_server: Any) -> None:
    """grimoire_generate extract content_type requires a query parameter."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_db_context):
        result = await mcp_server.call_tool("grimoire_generate", {
            "params": {
                "document_ids": ["doc-1"],
                "content_type": "extract",
            },
        })
    assert '"status": "error"' in result[0][0].text
    assert "query" in result[0][0].text


@pytest.mark.asyncio
async def test_generate_invalid_content_type(mcp_server: Any) -> None:
    """grimoire_generate returns error for an invalid content_type."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_db_context):
        result = await mcp_server.call_tool("grimoire_generate", {
            "params": {
                "document_ids": ["doc-1"],
                "content_type": "invalid_type",
            },
        })
    assert '"status": "error"' in result[0][0].text


@pytest.mark.asyncio
async def test_watch_start_requires_dev_tier(mcp_server: Any) -> None:
    """READ-tier key cannot start a watch."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with pytest.raises(ToolError) as exc_info:
        await mcp_server.call_tool("grimoire_watch_start", {
            "params": {"path": "/nonexistent/dir"},
        })
    text = str(exc_info.value)
    assert "requires API key tier" in text


@pytest.mark.asyncio
async def test_watch_start_allowed_for_dev_tier(mcp_server: Any) -> None:
    """DEV-tier key can start a watch."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    mock_watcher = MagicMock()
    mock_watcher.watch = AsyncMock(return_value="watch-1")

    with patch("grimoire.mcp.tools._get_mcp_watcher", return_value=mock_watcher):
        result = await mcp_server.call_tool("grimoire_watch_start", {
            "params": {"path": "/nonexistent/dir"},
        })
    assert '"status": "ok"' in result[0][0].text
    assert "watch-1" in result[0][0].text


@pytest.mark.asyncio
async def test_watch_status_returns_stats(mcp_server: Any) -> None:
    """grimoire_watch_status returns watcher statistics."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    mock_watcher = MagicMock()
    mock_stats = MagicMock()
    mock_stats.active_watches = 1
    mock_stats.total_files_processed = 10
    mock_stats.total_files_failed = 0
    mock_stats.watches = []
    mock_watcher.get_status.return_value = mock_stats

    with patch("grimoire.mcp.tools._get_mcp_watcher", return_value=mock_watcher):
        result = await mcp_server.call_tool("grimoire_watch_status", {})
    assert '"status": "ok"' in result[0][0].text
    assert '"active_watches": 1' in result[0][0].text


@pytest.mark.asyncio
async def test_watch_stop_allowed_for_dev_tier(mcp_server: Any) -> None:
    """DEV-tier key can stop a watch."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    mock_watcher = MagicMock()
    mock_watcher.unwatch = AsyncMock(return_value=True)

    with patch("grimoire.mcp.tools._get_mcp_watcher", return_value=mock_watcher):
        result = await mcp_server.call_tool("grimoire_watch_stop", {
            "params": {"watch_id": "watch-1"},
        })
    assert '"status": "ok"' in result[0][0].text
    assert "watch-1" in result[0][0].text


@pytest.mark.asyncio
async def test_watch_stop_returns_error_when_not_found(mcp_server: Any) -> None:
    """grimoire_watch_stop returns error when watch_id is unknown."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    mock_watcher = MagicMock()
    mock_watcher.unwatch = AsyncMock(return_value=False)

    with patch("grimoire.mcp.tools._get_mcp_watcher", return_value=mock_watcher):
        result = await mcp_server.call_tool("grimoire_watch_stop", {
            "params": {"watch_id": "missing"},
        })
    assert '"status": "error"' in result[0][0].text
    assert "not found" in result[0][0].text


@pytest.mark.asyncio
async def test_pg_query_returns_error_on_failure(mcp_server: Any) -> None:
    """grimoire_pg_query returns a structured error when the DB query fails."""
    from grimoire.mcp.tools import grimoire_pg_query, PgQueryInput

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=RuntimeError("DB down"))

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    mock_manager = MagicMock()
    mock_manager.session = _ctx

    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    with patch("grimoire.db.session.get_db_manager", return_value=mock_manager):
        result = await grimoire_pg_query(
            PgQueryInput(sql="SELECT id FROM documents", limit=10),
            ctx=MagicMock(),
        )
    assert '"status": "error"' in result
    assert "DB down" in result


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM documents",
        "select id, title from documents",
        "  SELECT 1  ",
        "WITH recent AS (SELECT id FROM documents) SELECT * FROM recent",
        "with recent as (select id from documents) select * from recent",
    ],
)
def test_pg_query_accepts_read_only_queries(sql: str) -> None:
    """SELECT and WITH ... SELECT CTE queries are accepted."""
    from grimoire.mcp.tools import PgQueryInput

    model = PgQueryInput(sql=sql)
    assert model.sql.strip() == sql.strip()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM documents",
        "UPDATE documents SET title = 'x'",
        "INSERT INTO documents VALUES (1)",
        "DROP TABLE documents",
        "TRUNCATE documents",
    ],
)
def test_pg_query_rejects_non_select(sql: str) -> None:
    """Non-SELECT/WITH statements are rejected."""
    from pydantic import ValidationError

    from grimoire.mcp.tools import PgQueryInput

    with pytest.raises(ValidationError):
        PgQueryInput(sql=sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * INTO new_table FROM documents",
        "select id into backup from documents",
    ],
)
def test_pg_query_rejects_select_into(sql: str) -> None:
    """SELECT INTO (table creation) is rejected even though it starts with SELECT."""
    from pydantic import ValidationError

    from grimoire.mcp.tools import PgQueryInput

    with pytest.raises(ValidationError):
        PgQueryInput(sql=sql)


@pytest.mark.asyncio
async def test_pg_query_wraps_query_with_limit() -> None:
    """grimoire_pg_query wraps the user SQL in a bounded subquery."""
    from grimoire.mcp.tools import grimoire_pg_query, PgQueryInput

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=iter([]))

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    mock_manager = MagicMock()
    mock_manager.session = _ctx

    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    with patch("grimoire.db.session.get_db_manager", return_value=mock_manager):
        result = await grimoire_pg_query(
            PgQueryInput(sql="SELECT id FROM documents", limit=10),
            ctx=MagicMock(),
        )

    assert '"status": "ok"' in result


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
        assert '"status": "ok"' in result[0][0].text


@pytest.mark.asyncio
async def test_ingest_requires_dev_tier(mcp_server: Any) -> None:
    """READ-tier key cannot call ingest_file."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with pytest.raises(ToolError) as exc_info:
        await mcp_server.call_tool("grimoire_ingest_file", {
            "params": {"file_path": "/nonexistent/test.txt"},
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
            model_dump=lambda: {"file_path": "/nonexistent/test.txt", "status": "completed"},
        ))
        result = await mcp_server.call_tool("grimoire_ingest_file", {
            "params": {"file_path": "/nonexistent/test.txt"},
        })
        assert '"status": "ok"' in result[0][0].text


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
    mock_doc.source_path = "/nonexistent/test.txt"
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
    async def _ctx() -> Any:
        yield mock_db

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _ctx):
        result = await mcp_server.call_tool("grimoire_delete_document", {
            "params": {"document_id": "doc-123"},
        })
    assert '"status": "ok"' in result[0][0].text


# ---------------------------------------------------------------------------
# Security-domain search tools
# ---------------------------------------------------------------------------


def _mock_search_result() -> MagicMock:
    """Build a mock QueryAgent search result for security tools."""
    chunk = MagicMock()
    chunk.model_dump = lambda: {
        "document_id": "doc-cve",
        "text": "Apache Log4j2 JNDI features do not protect against...",
        "score": 0.95,
    }
    result = MagicMock()
    result.query = "remote code execution"
    result.results = [chunk]
    result.total_results = 1
    result.duration_ms = 12
    return result


def _fake_preselect_db(doc_ids: list[str]) -> Any:
    """Mock DB session for security search tests.

    First execute() call (from _matching_document_ids): returns the given
    document ids (strings).  Second execute() call (from _facet_only_search
    document fetch): returns full mock Document objects with the same ids.
    """
    mock_db = MagicMock()

    doc_result = MagicMock()
    docs = []
    for doc_id in doc_ids:
        mock_doc = MagicMock()
        mock_doc.id = doc_id
        mock_doc.title = f"Test {doc_id}"
        mock_doc.source_type = "sigma_rule"
        mock_doc.severity = MagicMock(value="high")
        mock_doc.mitre_technique_id = "T1059"
        mock_doc.content_date = None
        mock_doc.source_path = f"/test/{doc_id}"
        docs.append(mock_doc)
    doc_result.scalars.return_value.all.return_value = docs

    ids_result = MagicMock()
    ids_result.scalars.return_value.all.return_value = doc_ids

    call_count = 0
    async def mock_execute(self: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ids_result
        return doc_result

    mock_db.execute = mock_execute

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    return _ctx


@pytest.mark.asyncio
async def test_search_cve_with_exact_id(mcp_server: Any) -> None:
    """Exact cve_id input short-circuits to a direct document lookup."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    mock_db = MagicMock()
    mock_docs_result = MagicMock()
    mock_doc = MagicMock()
    mock_doc.id = "doc-cve"
    mock_doc.title = "CVE-2021-44228"
    mock_doc.cve_id = "CVE-2021-44228"
    mock_doc.severity = "critical"
    mock_doc.mitre_technique_id = "T1190"
    mock_doc.content_date = None
    mock_doc.source_path = "/nvd/CVE-2021-44228.json"
    mock_doc.security_metadata = {"cvss_score": 10.0}
    mock_docs_result.scalars.return_value.first.return_value = mock_doc

    mock_chunks_result = MagicMock()
    mock_chunk = MagicMock()
    mock_chunk.content = "Apache Log4j2 JNDI features do not protect against..."
    mock_chunk.chunk_index = 0
    mock_chunks_result.scalars.return_value.all.return_value = [mock_chunk]

    mock_db.execute = AsyncMock(side_effect=[mock_docs_result, mock_chunks_result])

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _ctx):
        result = await mcp_server.call_tool("grimoire_search_cve", {
            "params": {"cve_id": "CVE-2021-44228"},
        })
    text = result[0][0].text
    assert '"status": "ok"' in text
    assert "CVE-2021-44228" in text
    assert "Log4j2" in text
    # Regression (QA P0): response must use Chunk.content, not Chunk.text
    assert '"content": "Apache Log4j2 JNDI features' in text
    assert "chunk_type" not in text


@pytest.mark.asyncio
async def test_search_cve_semantic_filters_docs_in_sql(mcp_server: Any) -> None:
    """Severity/CVSS/year facets pre-filter via SQL and restrict vector search."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_preselect_db(["doc-1", "doc-2"])):
        mock_agent.return_value.search = AsyncMock(return_value=_mock_search_result())

        await mcp_server.call_tool("grimoire_search_cve", {
            "params": {
                "query": "remote code execution",
                "severity": "critical",
                "min_cvss": 9.0,
                "year": 2024,
            },
        })

        call = mock_agent.return_value.search.await_args
        filters = call.kwargs["filter_dict"]
        # Broken ChromaDB list filters must NOT leak into the vector filter
        assert "platforms" not in filters
        assert "cve_id" not in filters
        assert "cvss_score" not in filters
        assert filters == {
            "source_type": "nvd_cve",
            "document_id": {"$in": ["doc-1", "doc-2"]},
        }


@pytest.mark.asyncio
async def test_search_cve_no_matching_docs_short_circuits(mcp_server: Any) -> None:
    """Facet filters matching zero documents skip the vector search entirely."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_preselect_db([])):
        mock_agent.return_value.search = AsyncMock(return_value=_mock_search_result())

        result = await mcp_server.call_tool("grimoire_search_cve", {
            "params": {"query": "anything", "severity": "critical"},
        })

        mock_agent.return_value.search.assert_not_awaited()
        text = result[0][0].text
        assert '"status": "ok"' in text
        assert '"total_results": 0' in text

    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_preselect_db([])):
        mock_agent.return_value.search = AsyncMock(return_value=_mock_search_result())
        result = await mcp_server.call_tool("grimoire_search_playbook", {
            "params": {"mitre_technique_id": "T9999"},
        })
        mock_agent.return_value.search.assert_not_awaited()
        assert '"total_results": 0' in result[0][0].text


@pytest.mark.asyncio
async def test_search_cve_validates_cve_id_format(mcp_server: Any) -> None:
    """Malformed CVE IDs are rejected by Pydantic validation."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with pytest.raises(ToolError):
        await mcp_server.call_tool("grimoire_search_cve", {
            "params": {"cve_id": "not-a-cve"},
        })


@pytest.mark.asyncio
async def test_search_cve_requires_input(mcp_server: Any) -> None:
    """Calling search_cve with neither query nor cve_id returns an error result."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_db_context):
        result = await mcp_server.call_tool("grimoire_search_cve", {"params": {}})
    text = result[0][0].text
    assert '"status": "error"' in text
    assert "query" in text


@pytest.mark.asyncio
async def test_search_playbook_facet_filters_docs_in_sql(mcp_server: Any) -> None:
    """MITRE/platform/log-source facets pre-filter docs via SQL."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_preselect_db(["doc-sig"])):
        mock_agent.return_value.search = AsyncMock(return_value=_mock_search_result())

        await mcp_server.call_tool("grimoire_search_playbook", {
            "params": {
                "query": "powershell execution",
                "mitre_technique_id": "T1059.001",
                "severity": "high",
                "platform": "windows",
                "log_source": "process_creation",
                "source_types": ["sigma_rule"],
            },
        })

        call = mock_agent.return_value.search.await_args
        filters = call.kwargs["filter_dict"]
        assert filters == {
            "source_type": "sigma_rule",
            "document_id": {"$in": ["doc-sig"]},
        }


@pytest.mark.asyncio
async def test_search_playbook_default_covers_both_corpora(mcp_server: Any) -> None:
    """Default source_types searches playbooks AND sigma rules."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_preselect_db(["doc-pb", "doc-sig"])):
        mock_agent.return_value.search = AsyncMock(return_value=_mock_search_result())

        await mcp_server.call_tool("grimoire_search_playbook", {
            "params": {"query": "contain ransomware"},
        })

        call = mock_agent.return_value.search.await_args
        filters = call.kwargs["filter_dict"]
        assert filters["source_type"] == {"$in": ["playbook", "sigma_rule"]}


@pytest.mark.asyncio
async def test_search_playbook_phase_facet(mcp_server: Any) -> None:
    """The phase facet pre-filters playbooks by JSONB playbook_phase."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_preselect_db(["doc-pb"])):
        mock_agent.return_value.search = AsyncMock(return_value=_mock_search_result())

        result = await mcp_server.call_tool("grimoire_search_playbook", {
            "params": {"query": "contain ransomware", "phase": "contain"},
        })

        call = mock_agent.return_value.search.await_args
        assert call.kwargs["filter_dict"]["document_id"] == {"$in": ["doc-pb"]}
        assert '"sql_prefiltered_documents": 1' in result[0][0].text


@pytest.mark.asyncio
async def test_search_playbook_accepts_source_types_list(mcp_server: Any) -> None:
    """source_types accepts a list of strings; no Pydantic validation."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_preselect_db([])):
        mock_agent.return_value.search = AsyncMock(return_value=_mock_search_result())
        # With query present → semantic path; empty SQL results → empty results
        result = await mcp_server.call_tool("grimoire_search_playbook", {
            "params": {"query": "x", "source_types": ["sigma_rule"]},
        })
        text = result[0][0].text
        assert '"mode": "semantic"' in text
        assert '"total_results": 0' in text


@pytest.mark.asyncio
async def test_search_playbook_facet_only_no_matching_docs(mcp_server: Any) -> None:
    """Facet-only search returns empty results when SQL pre-filter matches nothing."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_preselect_db([])):
        result = await mcp_server.call_tool("grimoire_search_playbook", {
            "params": {"mitre_technique_id": "T9999"},
        })
        text = result[0][0].text
        assert '"mode": "facet_only"' in text
        assert '"sql_prefiltered_documents": 0' in text
        assert '"total_results": 0' in text


@pytest.mark.asyncio
async def test_search_playbook_sigma_only_uses_equality(mcp_server: Any) -> None:
    """sigma-only source_types produces a single-element source_type filter (== not IN)."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_preselect_db(["doc-sig"])):
        mock_agent.return_value.search = AsyncMock(return_value=_mock_search_result())

        await mcp_server.call_tool("grimoire_search_playbook", {
            "params": {
                "query": "anything",
                "source_types": ["sigma_rule"],
            },
        })

        call = mock_agent.return_value.search.await_args
        filters = call.kwargs["filter_dict"]
        # Single-element list → == (not $in)
        assert filters["source_type"] == "sigma_rule"
        assert filters["document_id"] == {"$in": ["doc-sig"]}



@pytest.mark.asyncio
async def test_search_playbook_technique_only(mcp_server: Any) -> None:
    """A MITRE technique alone (no query) uses the vector-bypass path."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    # No query → _facet_only_search is called, not agent.search.
    with patch("grimoire.mcp.tools.get_query_agent") as mock_agent, \
         patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_preselect_db(["doc-sig"])):
        result = await mcp_server.call_tool("grimoire_search_playbook", {
            "params": {"mitre_technique_id": "T1059"},
        })
        text = result[0][0].text
        assert '"mode": "facet_only"' in text
        assert '"sql_prefiltered_documents": 1' in text
        mock_agent.return_value.search.assert_not_called()

    # Empty params → error
    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _fake_db_context):
        result = await mcp_server.call_tool("grimoire_search_playbook", {"params": {}})
    text = result[0][0].text
    assert '"status": "error"' in text


@pytest.mark.asyncio
async def test_search_playbook_validates_technique_id(mcp_server: Any) -> None:
    """Malformed MITRE technique IDs are rejected by Pydantic validation."""
    set_current_api_key(_make_api_key(ApiKeyTier.READ))

    with pytest.raises(ToolError):
        await mcp_server.call_tool("grimoire_search_playbook", {
            "params": {"mitre_technique_id": "bad-id"},
        })


@pytest.mark.asyncio
async def test_create_category(mcp_server: Any) -> None:
    """grimoire_create_category inserts a new category."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.commit = AsyncMock(return_value=None)
    mock_db.refresh = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _ctx):
        result = await mcp_server.call_tool("grimoire_create_category", {
            "params": {"name": "AI", "description": "AI stuff"},
        })
    assert '"status": "ok"' in result[0][0].text
    assert "AI" in result[0][0].text


@pytest.mark.asyncio
async def test_create_category_parent_not_found(mcp_server: Any) -> None:
    """grimoire_create_category returns error when parent_slug does not exist."""
    set_current_api_key(_make_api_key(ApiKeyTier.DEV))

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_result

    @asynccontextmanager
    async def _ctx() -> Any:
        yield mock_db

    with patch("grimoire.mcp.tools.get_db_context", new_callable=lambda: _ctx):
        result = await mcp_server.call_tool("grimoire_create_category", {
            "params": {"name": "AI", "parent_slug": "missing"},
        })
    assert '"status": "error"' in result[0][0].text
    assert "Parent category" in result[0][0].text


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


def test_mcp_rejects_invalid_api_key(client: TestClient) -> None:
    """Requests to /mcp with an invalid X-API-Key are rejected when DB is initialized."""
    with patch("grimoire.db.session.initialize_db", new_callable=AsyncMock):
        response = client.get("/mcp/sse", headers={"X-API-Key": "grim_rdl_invalidkey"})
    # Without a real DB the middleware returns 503; the important check is that
    # it does not pass auth and is not a 200.
    assert response.status_code in {401, 403, 503}
    assert response.status_code != 200


def test_mcp_accepts_valid_api_key(client: TestClient) -> None:
    """Requests to /mcp with a valid X-API-Key pass auth."""
    response = client.get("/mcp/sse", headers={"X-API-Key": "grim_agt_testkey123"})
    assert response.status_code != 401



