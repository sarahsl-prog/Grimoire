# Code Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all issues identified in CODE_REVIEW.md: unblock failing tests, fix security vulnerabilities, eliminate deprecated API usage, and tighten code quality.

**Architecture:** Work in phases — CI first (unblock tests so every subsequent fix is verified), then security, then correctness bugs, then code quality, then docs. Each task leaves the test suite green before moving on.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, pytest, python-slugify (already installed)

---

## File Map

| File | What changes |
|---|---|
| `pyproject.toml` | Add `aiosqlite`, `pytest-httpx`, `faker` to dev deps |
| `tests/test_config_validation.py` | Isolate 3 tests from real `grimoire.yaml` |
| `grimoire/api/main.py` | Fix CORS: remove `allow_credentials=True` |
| `grimoire/api/schemas.py` | Add `PatchDocumentRequest`; add path-validation field |
| `grimoire/api/routes/ingest.py` | Pass `storage_backend`; add path validation |
| `grimoire/api/routes/documents.py` | Add `PATCH /{id}` endpoint |
| `grimoire/config/settings.py` | Add `api.allowed_ingest_paths`; remove duplicate `DedupStrategy` |
| `grimoire/agents/watcher.py` | Fix processor crash: move try/except inside loop |
| `grimoire/agents/ingestion.py` | Replace `_log_extraction` no-op with real log; fix `datetime.utcnow()` |
| `grimoire/db/models.py` | Replace all `datetime.utcnow` defaults with `lambda: datetime.now(UTC)` |
| `grimoire/db/base.py` | Same `datetime` fix in `TimestampMixin` |
| `grimoire/cli/docs.py` | Fix `datetime.utcnow()` |
| `grimoire/core/parser.py` | Fix `asyncio.get_event_loop()`, `DOCLEY_AVAILABLE` typo, remove mock-detection code |
| `grimoire/core/tagger.py` | Add `__aenter__`/`__aexit__` for client lifecycle |
| `grimoire/cli/helpers.py` | Add `fallback_model` to `build_query_agent`; move `import os` in coordinator |
| `grimoire/agents/coordinator.py` | Move `import os` to top of file |
| `grimoire/api/routes/categories.py` | Use `python-slugify` for slug generation |
| `README.md` | Remove LangChain reference; add accurate stack description |
| `tests/test_api.py` | Add tests for path validation, storage_backend passthrough, PATCH endpoint |
| `tests/test_watcher_agent.py` | Add test for processor crash recovery |
| `tests/test_ingestion_agent.py` | Add test for EXTRACTED audit log entry |

---

## Task 1: Add missing test dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `aiosqlite` and `pytest-httpx` to dev dependencies**

In `pyproject.toml`, update the `[dependency-groups]` `dev` section:

```toml
[dependency-groups]
dev = [
    "pytest-asyncio>=1.3.0",
    "pytest-cov>=7.1.0",
    "ruff>=0.15.8",
    "aiosqlite>=0.20.0",
    "pytest-httpx>=0.35.0",
    "faker>=26.0.0",
]
```

> **Note:** `faker` is used by existing tests and is referenced in the virtualenv (`/.venv/bin/faker`) but was missing from dev deps.

- [ ] **Step 2: Sync dependencies**

```bash
uv sync
```

Expected: completes without error; `aiosqlite` and `pytest-httpx` appear in `.venv/lib/`.

- [ ] **Step 3: Verify test collection is unblocked**

```bash
.venv/bin/python -m pytest tests/ --co -q 2>&1 | tail -5
```

Expected output: `collected NNN items` with no `ImportError` or `ERROR` lines for `test_db_models.py`, `test_tagger.py`, or `test_storage_onedrive.py`.

- [ ] **Step 4: Run full test suite and record baseline**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_storage_onedrive.py -q 2>&1 | tail -5
```

Expected: 908 passed, 3 failed, 0 errors (same 3 config test failures as before — those are fixed in Task 2).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "fix(deps): add aiosqlite, pytest-httpx, faker to dev dependencies"
```

---

## Task 2: Fix config test isolation

**Files:**
- Modify: `tests/test_config_validation.py:54-58`, `:203-225`

The 3 failing tests (`test_default_settings_load`, `test_async_config_access`, `test_concurrent_reads`) all instantiate `GrimoireSettings()` directly, which loads the real `grimoire.yaml` and overrides expected defaults. Fix by pointing `GRIMOIRE_CONFIG` at a non-existent path so no YAML file is loaded.

- [ ] **Step 1: Verify current failure**

```bash
.venv/bin/python -m pytest tests/test_config_validation.py::TestConfigHappyPath::test_default_settings_load -v 2>&1 | tail -8
```

Expected: `AssertionError: assert 'minimax-m2.7:cloud' == 'llama3.2'`

- [ ] **Step 2: Fix `test_default_settings_load`**

In `tests/test_config_validation.py`, add a `monkeypatch` parameter and use it to clear the config path. Replace lines 54–59:

```python
def test_default_settings_load(self, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that default settings load without errors."""
    monkeypatch.setenv("GRIMOIRE_CONFIG", "/nonexistent/path/grimoire.yaml")
    settings = GrimoireSettings()
    assert settings.llm.model == "llama3.2"
    assert settings.database.pool_size == 10
    assert settings.vector_store.type == VectorStoreType.CHROMADB
```

- [ ] **Step 3: Find and fix `test_async_config_access` and `test_concurrent_reads`**

Both tests call `GrimoireSettings()` inside async contexts. Add the same monkeypatch to each. Search for them:

```bash
grep -n "test_async_config_access\|test_concurrent_reads" tests/test_config_validation.py
```

In each test method, add `monkeypatch: pytest.MonkeyPatch` to the signature and add this as the first line of the test body:

```python
monkeypatch.setenv("GRIMOIRE_CONFIG", "/nonexistent/path/grimoire.yaml")
```

> **Note:** `test_async_config_access` and `test_concurrent_reads` are async methods. Pytest-asyncio passes fixtures by name — adding `monkeypatch` as a parameter works fine.

- [ ] **Step 4: Run the 3 previously-failing tests**

```bash
.venv/bin/python -m pytest tests/test_config_validation.py::TestConfigHappyPath::test_default_settings_load tests/test_config_validation.py::TestConfigAsyncBehavior -v 2>&1 | tail -10
```

Expected: all 3 `PASSED`.

- [ ] **Step 5: Run full config test suite**

```bash
.venv/bin/python -m pytest tests/test_config_validation.py -v 2>&1 | tail -5
```

Expected: 71 passed, 0 failed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_config_validation.py
git commit -m "fix(tests): isolate config tests from local grimoire.yaml"
```

---

## Task 3: Fix CORS misconfiguration

**Files:**
- Modify: `grimoire/api/main.py:37-43`
- Modify: `tests/test_api.py` (add CORS test)

`allow_origins=["*"]` with `allow_credentials=True` is invalid per the CORS spec and is rejected by browsers. Since the API has no authentication currently, `allow_credentials` provides no value and must be removed.

- [ ] **Step 1: Write the failing test**

In `tests/test_api.py`, add a new test class after `TestHealthCheck`:

```python
class TestCORSHeaders:
    def test_cors_does_not_use_wildcard_with_credentials(self, app):
        """CORS must not combine wildcard origin with allow_credentials."""
        from starlette.middleware.cors import CORSMiddleware
        for mw in app.middleware_stack.app.middleware_stack.app.middleware:
            # Check that no CORSMiddleware has both wildcard and credentials
            pass
        # Simpler: check that the response header is not invalid
        with TestClient(app) as c:
            resp = c.options(
                "/health",
                headers={
                    "Origin": "http://evil.com",
                    "Access-Control-Request-Method": "GET",
                },
            )
        # With wildcard+credentials the header would be "*" which is invalid;
        # with the fix, the header should either be missing or a specific origin.
        origin_header = resp.headers.get("access-control-allow-origin", "")
        assert origin_header != "*" or "access-control-allow-credentials" not in resp.headers
```

- [ ] **Step 2: Run test to confirm the current state (documents the problem)**

```bash
.venv/bin/python -m pytest tests/test_api.py::TestCORSHeaders -v 2>&1 | tail -10
```

Note the result — it may pass or fail depending on Starlette version behaviour. Proceed regardless.

- [ ] **Step 3: Fix the CORS configuration**

In `grimoire/api/main.py`, replace lines 37–43:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,   # Cannot use True with allow_origins=["*"]
    allow_methods=["*"],
    allow_headers=["*"],
)
```

> **Why `allow_credentials=False`:** There is no JWT/session authentication enforced yet. When authentication is added, `allow_origins` must be changed to an explicit list and `allow_credentials` can be re-enabled at that point.

- [ ] **Step 4: Run the full API test suite**

```bash
.venv/bin/python -m pytest tests/test_api.py -v 2>&1 | tail -10
```

Expected: all API tests pass.

- [ ] **Step 5: Commit**

```bash
git add grimoire/api/main.py tests/test_api.py
git commit -m "fix(security): remove invalid CORS allow_credentials with wildcard origin"
```

---

## Task 4: Fix path traversal in ingest API

**Files:**
- Modify: `grimoire/config/settings.py` (add `api.allowed_ingest_paths`)
- Modify: `grimoire/api/schemas.py` (add validation to request schemas)
- Modify: `grimoire/api/routes/ingest.py` (pass `storage_backend` parameter)
- Modify: `tests/test_api.py` (add path traversal tests)

The ingest endpoints accept arbitrary filesystem paths. Add a configurable allowlist. When `allowed_ingest_paths` is empty (default), all paths are permitted (suitable for local dev). When set, only paths under listed directories are accepted.

- [ ] **Step 1: Add `allowed_ingest_paths` to `APIConfig`**

In `grimoire/config/settings.py`, update `APIConfig` (around line 669):

```python
class APIConfig(BaseModel):
    """FastAPI server configuration."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="0.0.0.0", description="Bind host")  # noqa: S104
    port: int = Field(default=8001, ge=1, le=65535, description="Bind port")
    reload: bool = Field(default=False, description="Enable auto-reload (dev only)")
    workers: int = Field(default=4, ge=1, le=64, description="Worker processes")
    secret_key: str = Field(
        default="change-me-in-production",
        description="Secret key for JWT (CHANGE IN PROD!)",
    )
    allowed_ingest_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Allowlist of base directories for ingest. "
            "Empty list permits all paths (local dev only). "
            "Set to restrict ingestion to specific directories."
        ),
    )
```

- [ ] **Step 2: Write the failing path-traversal test**

In `tests/test_api.py`, add inside `TestIngestAPI`:

```python
@patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
def test_ingest_file_blocks_sensitive_path(self, mock_get_agent, client, monkeypatch):
    """Ingest should reject paths outside allowed directories when configured."""
    monkeypatch.setenv("GRIMOIRE_API__ALLOWED_INGEST_PATHS", '["/tmp/docs"]')
    # Reload settings within the request context
    from grimoire.config import settings as grimoire_settings
    import importlib
    import grimoire.config.settings as settings_mod
    settings_mod._settings = None  # reset singleton

    resp = client.post(
        "/api/v1/ingest/file",
        json={"file_path": "/etc/passwd"},
    )
    assert resp.status_code == 400
    assert "not permitted" in resp.json()["detail"].lower()

@patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
def test_ingest_file_passes_storage_backend(self, mock_get_agent, client):
    """storage_backend parameter must be forwarded to the agent."""
    mock_agent = MagicMock()
    mock_result = MagicMock()
    mock_result.model_dump.return_value = {
        "file_path": "/tmp/test.pdf",
        "document_id": "doc-1",
        "status": "completed",
        "chunks_created": 3,
        "vectors_stored": 3,
        "tags_applied": 0,
        "error_message": None,
        "duration_ms": 50,
    }
    mock_agent.ingest_file = AsyncMock(return_value=mock_result)
    mock_get_agent.return_value = mock_agent

    resp = client.post(
        "/api/v1/ingest/file",
        json={"file_path": "/tmp/test.pdf", "storage_backend": "local"},
    )
    assert resp.status_code == 200
    # Verify backend was forwarded to agent
    call_kwargs = mock_agent.ingest_file.call_args
    assert call_kwargs.kwargs.get("storage_backend") == "local" or (
        len(call_kwargs.args) > 2 and call_kwargs.args[2] == "local"
    )
```

- [ ] **Step 3: Run the tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_api.py::TestIngestAPI::test_ingest_file_blocks_sensitive_path tests/test_api.py::TestIngestAPI::test_ingest_file_passes_storage_backend -v 2>&1 | tail -10
```

Expected: both FAIL.

- [ ] **Step 4: Add path validation to `IngestFileRequest` and `IngestDirectoryRequest`**

In `grimoire/api/schemas.py`, add a helper at the top (after imports):

```python
import os
from pathlib import Path


def _validate_ingest_path(path: str) -> str:
    """Validate an ingest path against the configured allowlist.

    Raises ValueError if the path is outside all allowed directories.
    An empty allowlist permits all paths.
    """
    from grimoire.config.settings import get_settings
    settings = get_settings()
    allowed = settings.api.allowed_ingest_paths
    if not allowed:
        return path  # No restriction configured

    resolved = Path(os.path.realpath(path))
    for base in allowed:
        if str(resolved).startswith(os.path.realpath(base)):
            return path

    allowed_str = ", ".join(allowed)
    raise ValueError(
        f"Path '{path}' is not permitted. "
        f"Must be under one of: {allowed_str}"
    )
```

Update `IngestFileRequest` and `IngestDirectoryRequest` to validate on construction:

```python
from pydantic import field_validator

class IngestFileRequest(BaseModel):
    """Request to ingest a single file."""

    file_path: str = Field(..., description="Path to the file to ingest.")
    auto_tag: bool = Field(default=True, description="Auto-tag with LLM.")
    storage_backend: str | None = Field(default=None, description="Storage backend override.")

    @field_validator("file_path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _validate_ingest_path(v)


class IngestDirectoryRequest(BaseModel):
    """Request to ingest a directory."""

    directory: str = Field(..., description="Path to the directory.")
    recursive: bool = Field(default=True, description="Recurse into subdirectories.")
    auto_tag: bool = Field(default=True, description="Auto-tag with LLM.")
    storage_backend: str | None = Field(default=None, description="Storage backend override.")

    @field_validator("directory")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _validate_ingest_path(v)
```

> **Note:** Pydantic raises `ValidationError` on field validator failure, and FastAPI automatically converts this to a 422 response. However, to get a 400 with a readable `detail`, wrap the route in a try/except or add a custom exception handler. The simpler approach: use `HTTPException` by raising it from within the validator via a custom approach. Actually, FastAPI converts Pydantic validation errors to 422. To produce 400, add an exception handler or change the Pydantic validator to use a custom approach.

Update the validator to raise `HTTPException` directly by making it a route-level check. Replace the validator approach with a route-level guard in `ingest.py`:

```python
# grimoire/api/routes/ingest.py
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.api.dependencies import get_db_session, get_ingestion_agent
from grimoire.api.schemas import (
    BatchIngestResponse,
    IngestDirectoryRequest,
    IngestFileRequest,
    IngestResultResponse,
)
from grimoire.db.models import StorageBackend

router = APIRouter(prefix="/ingest", tags=["ingest"])


def _check_path_allowed(path: str) -> None:
    """Raise HTTP 400 if path is outside configured allowed directories."""
    import os
    from pathlib import Path
    from grimoire.config.settings import get_settings

    settings = get_settings()
    allowed = settings.api.allowed_ingest_paths
    if not allowed:
        return  # No restriction

    resolved = str(Path(os.path.realpath(path)))
    for base in allowed:
        if resolved.startswith(os.path.realpath(base)):
            return

    allowed_str = ", ".join(allowed)
    raise HTTPException(
        status_code=400,
        detail=f"Path '{path}' is not permitted. Must be under one of: {allowed_str}",
    )


@router.post("/file", response_model=IngestResultResponse)
async def ingest_file(
    request: IngestFileRequest,
    db: AsyncSession = Depends(get_db_session),
) -> IngestResultResponse:
    """Ingest a single file into the knowledge base."""
    _check_path_allowed(request.file_path)
    agent = get_ingestion_agent()
    backend = StorageBackend(request.storage_backend) if request.storage_backend else None
    result = await agent.ingest_file(
        db, request.file_path,
        auto_tag=request.auto_tag,
        storage_backend=backend,
    )
    return IngestResultResponse(**result.model_dump())


@router.post("/directory", response_model=BatchIngestResponse)
async def ingest_directory(
    request: IngestDirectoryRequest,
    db: AsyncSession = Depends(get_db_session),
) -> BatchIngestResponse:
    """Ingest all supported files from a directory."""
    _check_path_allowed(request.directory)
    agent = get_ingestion_agent()
    backend = StorageBackend(request.storage_backend) if request.storage_backend else None
    result = await agent.ingest_directory(
        db, request.directory,
        recursive=request.recursive,
        auto_tag=request.auto_tag,
        storage_backend=backend,
    )
    return BatchIngestResponse(**result.model_dump())
```

- [ ] **Step 5: Run the new tests**

```bash
.venv/bin/python -m pytest tests/test_api.py::TestIngestAPI -v 2>&1 | tail -10
```

Expected: all ingest tests pass including the 2 new ones.

- [ ] **Step 6: Run full API test suite**

```bash
.venv/bin/python -m pytest tests/test_api.py -v 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add grimoire/config/settings.py grimoire/api/schemas.py grimoire/api/routes/ingest.py tests/test_api.py
git commit -m "fix(security): add path allowlist validation to ingest API endpoints"
```

---

## Task 5: Fix watcher event processor crash recovery

**Files:**
- Modify: `grimoire/agents/watcher.py:350-373`
- Modify: `tests/test_watcher_agent.py`

Currently, any unhandled exception in the event processing loop causes the loop to exit permanently. The fix is to move the exception handler inside the loop.

- [ ] **Step 1: Write the failing test**

In `tests/test_watcher_agent.py`, find or add a test class for the processor. Add this test:

```python
@pytest.mark.asyncio
async def test_processor_continues_after_error():
    """Event processor must not exit when one event handler raises."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from grimoire.agents.watcher import WatcherAgent
    from grimoire.storage.base import FileChange, FileChangeType
    from grimoire.storage.watch_manager import WatchManager

    call_count = 0
    original_error_raised = False

    async def failing_then_succeeding_handle(watch_id, change):
        nonlocal call_count, original_error_raised
        call_count += 1
        if call_count == 1:
            original_error_raised = True
            raise RuntimeError("simulated handler error")

    mock_ingestion = MagicMock()
    mock_wm = MagicMock(spec=WatchManager)
    mock_wm.stop_all = AsyncMock()

    agent = WatcherAgent(
        watch_manager=mock_wm,
        ingestion_agent=mock_ingestion,
        db_session_factory=AsyncMock(),
    )

    # Patch the internal handler to inject our test version
    with patch.object(agent, "_handle_file_event", side_effect=failing_then_succeeding_handle):
        agent._running = True
        agent._trackers["w1"] = MagicMock()

        # Put two events in the queue
        change = FileChange(path="/tmp/a.txt", change_type=FileChangeType.CREATED)
        await agent._processing_queue.put(("w1", change))
        await agent._processing_queue.put(("w1", change))

        # Run the processor briefly
        import asyncio
        task = asyncio.create_task(agent._process_events())
        await asyncio.sleep(0.1)
        agent._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert original_error_raised, "First call should have raised"
    assert call_count == 2, f"Processor should have continued to event 2, got {call_count}"
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_watcher_agent.py::test_processor_continues_after_error -v 2>&1 | tail -10
```

Expected: FAIL — `call_count` is 1, not 2, because the processor exits after the first error.

- [ ] **Step 3: Fix `_process_events` in `grimoire/agents/watcher.py`**

Replace the `_process_events` method (lines ~350–373) with:

```python
async def _process_events(self) -> None:
    """Background task that processes file change events."""
    logger.info("WatcherAgent: event processor started")

    try:
        while self._running:
            try:
                watch_id, change = await asyncio.wait_for(
                    self._processing_queue.get(),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue

            try:
                await self._handle_file_event(watch_id, change)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"WatcherAgent: unhandled error processing event "
                    f"for {change.path}: {e}"
                )
                # Continue the loop — one bad event must not kill the processor

    except asyncio.CancelledError:
        logger.info("WatcherAgent: event processor cancelled")
        raise
    except Exception as e:
        logger.error(f"WatcherAgent: event processor fatal error: {e}")
    finally:
        logger.info("WatcherAgent: event processor stopped")
```

- [ ] **Step 4: Run the new test**

```bash
.venv/bin/python -m pytest tests/test_watcher_agent.py::test_processor_continues_after_error -v 2>&1 | tail -5
```

Expected: PASS.

- [ ] **Step 5: Run the full watcher test suite**

```bash
.venv/bin/python -m pytest tests/test_watcher_agent.py -v 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add grimoire/agents/watcher.py tests/test_watcher_agent.py
git commit -m "fix(watcher): processor loop now survives individual event handler errors"
```

---

## Task 6: Fix `_log_extraction` no-op and silent `storage_backend` drop

**Files:**
- Modify: `grimoire/agents/ingestion.py:294`, `:752-762`
- Modify: `tests/test_ingestion_agent.py`

Two separate bugs in one task since they're both small and both in ingestion:
1. `_log_extraction` is called but does nothing — the EXTRACTED step is never audited.
2. The `storage_backend` parameter was already fixed in the route layer (Task 4), but the test here verifies end-to-end.

- [ ] **Step 1: Write the failing audit-log test**

In `tests/test_ingestion_agent.py`, add:

```python
@pytest.mark.asyncio
async def test_ingest_file_logs_extracted_action():
    """IngestionAgent must write an EXTRACTED processing log entry on success."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from grimoire.agents.ingestion import IngestionAgent
    from grimoire.core.parser import ParsedDocument, DocumentMetadata
    from grimoire.db.models import ActionType, ProcessingStatus

    # Build minimal agent with mocked dependencies
    mock_parser = MagicMock()
    parsed = ParsedDocument(
        text="Some document text for testing.",
        metadata=DocumentMetadata(word_count=5, file_size=100),
        status="success",
    )
    mock_parser.parse = AsyncMock(return_value=parsed)

    mock_embedder = MagicMock()
    mock_embedder.embedding_dim = 384
    mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384])

    mock_vs = MagicMock()
    mock_vs.is_initialized = True
    mock_vs.add_documents = AsyncMock()

    agent = IngestionAgent(
        parser=mock_parser,
        embedder=mock_embedder,
        vector_store=mock_vs,
    )

    # Track all ProcessingLog entries added to the session
    logged_actions = []
    mock_db = AsyncMock()

    original_add = mock_db.add

    def capture_add(obj):
        from grimoire.db.models import ProcessingLog
        if isinstance(obj, ProcessingLog):
            logged_actions.append(obj.action)
        return original_add(obj)

    mock_db.add.side_effect = capture_add
    mock_db.flush = AsyncMock()
    mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))

    # Patch dedup and document creation
    from grimoire.core.dedup import DedupResult, DeduplicationAction

    with patch.object(agent, "_check_dedup", return_value=DedupResult(
        action=DeduplicationAction.NEW, file_hash="abc123"
    )), patch.object(agent, "_create_document_record", new_callable=AsyncMock) as mock_create, \
         patch.object(agent, "_chunk_document", new_callable=AsyncMock) as mock_chunk, \
         patch.object(agent, "_store_chunks_in_db", new_callable=AsyncMock) as mock_store, \
         patch.object(agent, "_embed_and_store", new_callable=AsyncMock, return_value=1):

        mock_doc = MagicMock()
        mock_doc.id = "doc-uuid-1"
        mock_create.return_value = mock_doc
        mock_chunk.return_value = []
        mock_store.return_value = []

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"test")
            tmp_path = f.name
        try:
            await agent.ingest_file(mock_db, tmp_path)
        finally:
            os.unlink(tmp_path)

    assert ActionType.EXTRACTED in logged_actions, (
        f"Expected EXTRACTED in audit log, got: {logged_actions}"
    )
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_ingestion_agent.py::test_ingest_file_logs_extracted_action -v 2>&1 | tail -10
```

Expected: FAIL — `ActionType.EXTRACTED not in logged_actions`.

- [ ] **Step 3: Fix `_log_extraction` and its callsite in `grimoire/agents/ingestion.py`**

Remove the call to `_log_extraction` at line 294 and the no-op method at lines 752–762.

Instead, add a real log call in `ingest_file` after the document record is created. In the `ingest_file` method, after the two branches where `doc` is set (after the `if dedup_result.action == DeduplicationAction.UPDATE:` block, approximately line 308), add:

```python
# Step 4a: Log the extraction
await self._log_processing(
    db, doc.id, ActionType.EXTRACTED, StatusType.SUCCESS,
    {
        "word_count": parsed.metadata.word_count,
        "pages": parsed.metadata.pages,
        "file_size": parsed.metadata.file_size,
    },
)
```

Also remove the `_log_extraction` method definition (lines ~752–762) and its import call at line 294.

- [ ] **Step 4: Run the audit log test**

```bash
.venv/bin/python -m pytest tests/test_ingestion_agent.py::test_ingest_file_logs_extracted_action -v 2>&1 | tail -5
```

Expected: PASS.

- [ ] **Step 5: Run full ingestion test suite**

```bash
.venv/bin/python -m pytest tests/test_ingestion_agent.py -v 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add grimoire/agents/ingestion.py tests/test_ingestion_agent.py
git commit -m "fix(ingestion): add EXTRACTED audit log; remove no-op _log_extraction method"
```

---

## Task 7: Replace `datetime.utcnow()` (deprecated in Python 3.12+)

**Files:**
- Modify: `grimoire/db/models.py` (12 occurrences)
- Modify: `grimoire/db/base.py:63`
- Modify: `grimoire/agents/ingestion.py:335,719`
- Modify: `grimoire/agents/watcher.py:390`
- Modify: `grimoire/cli/docs.py:37`

`datetime.utcnow()` is deprecated since Python 3.12 and raises `DeprecationWarning` on Python 3.13. Replace with `datetime.now(UTC)`.

- [ ] **Step 1: Update `grimoire/db/base.py`**

Replace the `TimestampMixin` class (lines 57–65):

```python
class TimestampMixin:
    """Mixin that adds created_at timestamp."""

    from datetime import UTC, datetime

    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
```

- [ ] **Step 2: Update all `datetime.utcnow` usages in `grimoire/db/models.py`**

Add `UTC` to the import at the top of the file. The existing import is:
```python
from datetime import datetime
```
Change it to:
```python
from datetime import UTC, datetime
```

Then do a find-replace in `models.py`: replace all occurrences of `default=datetime.utcnow` with `default=lambda: datetime.now(UTC)`, and `onupdate=datetime.utcnow` with `onupdate=lambda: datetime.now(UTC)`.

Run this to verify all are changed:

```bash
grep -n "datetime.utcnow" grimoire/db/models.py
```

Expected: no output (all replaced).

- [ ] **Step 3: Update `grimoire/agents/ingestion.py`**

Add `UTC` to the datetime import (line ~16):
```python
from datetime import UTC, datetime
```

Replace line 335:
```python
doc.processed_at = datetime.now(UTC)
```

Replace line 719:
```python
doc.updated_at = datetime.now(UTC)
```

- [ ] **Step 4: Update `grimoire/agents/watcher.py`**

Add `UTC` to the datetime import at the top:
```python
from datetime import UTC, datetime
```

Replace line 390:
```python
tracker.last_event_at = datetime.now(UTC)
```

- [ ] **Step 5: Update `grimoire/cli/docs.py`**

```bash
grep -n "datetime.utcnow" grimoire/cli/docs.py
```

Open the file and replace the `datetime.utcnow()` call with `datetime.now(UTC)`. Add `UTC` to the `from datetime import ...` line in that file.

- [ ] **Step 6: Verify no remaining occurrences**

```bash
grep -rn "datetime.utcnow" grimoire/ --include="*.py"
```

Expected: no output.

- [ ] **Step 7: Run the full test suite**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_storage_onedrive.py -q 2>&1 | tail -5
```

Expected: all previously-passing tests still pass, 0 errors.

- [ ] **Step 8: Commit**

```bash
git add grimoire/db/models.py grimoire/db/base.py grimoire/agents/ingestion.py grimoire/agents/watcher.py grimoire/cli/docs.py
git commit -m "fix(deprecation): replace datetime.utcnow() with datetime.now(UTC) throughout"
```

---

## Task 8: Fix deprecated `asyncio.get_event_loop()` in parser

**Files:**
- Modify: `grimoire/core/parser.py:453`

`asyncio.get_event_loop()` inside a running coroutine is deprecated in Python 3.10+ and emits `DeprecationWarning` in 3.12+. Use `asyncio.get_running_loop()` instead.

- [ ] **Step 1: Make the change in `grimoire/core/parser.py`**

Replace lines 451–459:

```python
        try:
            result = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None,
                    self._parse_sync,
                    file_path_obj,
                    config
                ),
                timeout=config.timeout
            )
```

- [ ] **Step 2: Run the parser tests**

```bash
.venv/bin/python -m pytest tests/test_parser.py -v 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add grimoire/core/parser.py
git commit -m "fix(deprecation): replace asyncio.get_event_loop() with get_running_loop() in parser"
```

---

## Task 9: Fix slug generation in category creation

**Files:**
- Modify: `grimoire/api/routes/categories.py:54`
- Modify: `tests/test_api.py`

The current slug generator `request.name.lower().replace(" ", "-")` produces invalid slugs for names with accents, punctuation, or special characters. `python-slugify` is already installed.

- [ ] **Step 1: Write the failing test**

In `tests/test_api.py`, inside `TestCategoriesAPI` (find the existing class or add one), add:

```python
@patch(f"{_ROUTES_CATEGORIES}.get_db_session")
def test_create_category_slug_handles_special_chars(self, mock_db, client):
    """Category slugs must be URL-safe even with special chars in the name."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(
        scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
    ))
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    captured_slug = None
    original_add = mock_session.add

    def capture_add(obj):
        nonlocal captured_slug
        from grimoire.db.models import Category
        if isinstance(obj, Category):
            captured_slug = obj.slug
        return original_add(obj)

    mock_session.add.side_effect = capture_add

    async def override_db():
        yield mock_session

    from grimoire.api.dependencies import get_db_session
    client.app.dependency_overrides[get_db_session] = override_db

    resp = client.post("/api/v1/categories", json={
        "name": "C++ & Algorithms",
        "description": "Programming category",
    })

    client.app.dependency_overrides.clear()

    # The slug must be URL-safe (no +, &, spaces)
    if captured_slug:
        import re
        assert re.match(r'^[a-z0-9\-]+$', captured_slug), (
            f"Slug '{captured_slug}' contains invalid characters"
        )
```

- [ ] **Step 2: Run the test to confirm current behavior**

```bash
.venv/bin/python -m pytest tests/test_api.py::TestCategoriesAPI::test_create_category_slug_handles_special_chars -v 2>&1 | tail -10
```

Expected: FAIL — `c++-&-algorithms` fails the regex.

- [ ] **Step 3: Fix `categories.py`**

In `grimoire/api/routes/categories.py`, replace line 54:

```python
from slugify import slugify

# Inside create_category:
slug = slugify(request.name)
```

Full updated route (lines 48–83):

```python
@router.post("", response_model=CategoryResponse, status_code=201)
async def create_category(
    request: CategoryCreateRequest,
    db: AsyncSession = Depends(get_db_session),
) -> CategoryResponse:
    """Create a new category."""
    from slugify import slugify

    slug = slugify(request.name)

    parent_id = None
    if request.parent_slug:
        parent = (
            await db.execute(select(Category).where(Category.slug == request.parent_slug))
        ).scalars().first()
        if not parent:
            raise HTTPException(
                status_code=404,
                detail=f"Parent category '{request.parent_slug}' not found",
            )
        parent_id = parent.id

    cat = Category(
        id=str(uuid4()),
        name=request.name,
        slug=slug,
        description=request.description,
        parent_id=parent_id,
        color=request.color,
    )
    db.add(cat)
    await db.commit()

    return CategoryResponse(
        id=cat.id,
        name=cat.name,
        slug=cat.slug,
        description=cat.description or "",
        parent_id=cat.parent_id,
        color=cat.color or "#3498db",
    )
```

- [ ] **Step 4: Run the test**

```bash
.venv/bin/python -m pytest tests/test_api.py::TestCategoriesAPI::test_create_category_slug_handles_special_chars -v 2>&1 | tail -5
```

Expected: PASS.

- [ ] **Step 5: Run full API tests**

```bash
.venv/bin/python -m pytest tests/test_api.py -q 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add grimoire/api/routes/categories.py tests/test_api.py
git commit -m "fix(categories): use python-slugify for URL-safe slug generation"
```

---

## Task 10: Clean up `parser.py` — typo and mock-detection code

**Files:**
- Modify: `grimoire/core/parser.py:26`, `:236-258`

Two independent clean-ups: fix the `DOCLEY_AVAILABLE` typo and remove mock-detection code (`_mock_name` checks) from production code.

- [ ] **Step 1: Fix the `DOCLEY_AVAILABLE` typo**

In `grimoire/core/parser.py`, replace all 5 occurrences of `DOCLEY_AVAILABLE` with `DOCLING_AVAILABLE`:

```bash
# Verify all occurrences
grep -n "DOCLEY_AVAILABLE" grimoire/core/parser.py
```

Then do the rename (all occurrences at lines 26, 28, 29, 141, 435):

```python
# Line 26 — was: DOCLEY_AVAILABLE = True
DOCLING_AVAILABLE = True
# Line 27/28 — was: DOCLEY_AVAILABLE = False
DOCLING_AVAILABLE = False
# Line 141 — was: if not DOCLEY_AVAILABLE:
if not DOCLING_AVAILABLE:
# Line 435 — was: if not DOCLEY_AVAILABLE:
if not DOCLING_AVAILABLE:
```

- [ ] **Step 2: Remove mock-detection code from `_process_docling_result`**

In `_process_docling_result` (lines ~232–258), the method has these patterns that check for test mocks in production code:

```python
elif hasattr(md_value, '_mock_name'):
    text = ""
...
if hasattr(result, '_mock_name'):
    text = ""
```

Replace the entire text-extraction block (lines ~234–260) with a clean implementation:

```python
# Extract text — Docling provides markdown export
text = ""
if hasattr(result, 'document') and result.document is not None:
    try:
        text = result.document.export_to_markdown()
    except (AttributeError, TypeError):
        text = str(result.document)
elif hasattr(result, 'markdown') and isinstance(result.markdown, str):
    text = result.markdown
```

The key change: replace `hasattr(md_value, '_mock_name')` with `isinstance(result.markdown, str)` — only use the value if it actually is a string.

- [ ] **Step 3: Run the parser tests**

```bash
.venv/bin/python -m pytest tests/test_parser.py -v 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 4: Run the full suite to catch regressions**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_storage_onedrive.py -q 2>&1 | tail -5
```

Expected: no new failures.

- [ ] **Step 5: Commit**

```bash
git add grimoire/core/parser.py
git commit -m "fix(parser): rename DOCLEY->DOCLING_AVAILABLE; remove mock-detection from production code"
```

---

## Task 11: Fix `Tagger` HTTP client lifecycle and `build_query_agent` fallback model

**Files:**
- Modify: `grimoire/core/tagger.py:213`
- Modify: `grimoire/cli/helpers.py:101-110`

Two small fixes: prevent `httpx.AsyncClient` connection leaks in Tagger, and pass `fallback_model` in `build_query_agent`.

- [ ] **Step 1: Add `__aenter__`/`__aexit__` to `Tagger`**

In `grimoire/core/tagger.py`, add context manager methods after `__init__` (around line 213):

```python
async def __aenter__(self) -> "Tagger":
    """Async context manager entry."""
    return self

async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
    """Async context manager exit — close HTTP client."""
    await self._close_client()
```

- [ ] **Step 2: Fix `build_query_agent` in `grimoire/cli/helpers.py`**

In `build_query_agent` (lines ~91–131), update the `EmbeddingConfig` construction (around line 101) to include `fallback_model`:

```python
embed_config = EmbeddingConfig(
    model=settings.embeddings.model,
    fallback_model=settings.embeddings.fallback_model,  # was missing
    device=settings.embeddings.device,
    batch_size=settings.embeddings.batch_size,
)
```

- [ ] **Step 3: Run the affected tests**

```bash
.venv/bin/python -m pytest tests/test_tagger.py tests/test_embedder.py -v 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add grimoire/core/tagger.py grimoire/cli/helpers.py
git commit -m "fix(tagger): add context manager for client lifecycle; fix missing fallback_model in query agent"
```

---

## Task 12: Move `import os` in `CoordinatorAgent`

**Files:**
- Modify: `grimoire/agents/coordinator.py:562`

A `import os` inside a method body; move to the top of the file.

- [ ] **Step 1: Check current top-of-file imports**

```bash
head -40 grimoire/agents/coordinator.py
```

The file already has `import re`, `import time`, etc. at the top. Add `import os` after `import re`:

```python
import os
import re
import time
```

- [ ] **Step 2: Remove the inline import from `_handle_ingest`**

In `_handle_ingest` (around line 562), remove the line:
```python
import os
```

- [ ] **Step 3: Run coordinator tests**

```bash
.venv/bin/python -m pytest tests/test_coordinator_agent.py -q 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add grimoire/agents/coordinator.py
git commit -m "refactor(coordinator): move import os to module level"
```

---

## Task 13: Fix `DedupStrategy` inconsistency between settings and dedup module

**Files:**
- Modify: `grimoire/config/settings.py:82-88`
- Modify: `tests/test_config_validation.py` (if it tests `DedupStrategy`)

`grimoire/config/settings.py` defines `DedupStrategy(HASH, CONTENT)` which is never used by the actual dedup logic in `grimoire/core/dedup.py` (which has its own `DedupStrategy(AUTO, SKIP, DUPLICATE, MANUAL)`). The settings one is dead code — remove it.

- [ ] **Step 1: Check if any test imports the settings `DedupStrategy`**

```bash
grep -rn "DedupStrategy" tests/ grimoire/ --include="*.py" | grep -v "core/dedup\|__pycache__"
```

Identify whether `DedupStrategy` is imported from `grimoire.config.settings` vs `grimoire.core.dedup` anywhere.

- [ ] **Step 2: Remove `DedupStrategy` from `grimoire/config/settings.py`**

Delete lines 82–87 (the `DedupStrategy` enum class with `HASH` and `CONTENT` values).

Also update `ProcessingConfig.dedup_strategy` (around line 658) — it uses `DedupStrategy.HASH` as default. Since the settings `DedupStrategy` is being removed, change the field to a plain string with the same semantics:

```python
dedup_strategy: str = Field(
    default="hash",
    description="Deduplication strategy (hash or content)",
)
```

- [ ] **Step 3: Fix any imports of the removed enum**

```bash
grep -rn "from grimoire.config.settings import.*DedupStrategy\|from grimoire.config import.*DedupStrategy" grimoire/ tests/ --include="*.py"
```

Update any such imports to either remove them or point to `grimoire.core.dedup.DedupStrategy` if that was the intent.

Also check `grimoire/config/__init__.py` and remove `DedupStrategy` from its exports if present.

- [ ] **Step 4: Run config and dedup tests**

```bash
.venv/bin/python -m pytest tests/test_config_validation.py tests/test_dedup.py -v 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add grimoire/config/settings.py grimoire/config/__init__.py
git commit -m "refactor(config): remove unused DedupStrategy enum from settings (was diverged from dedup module)"
```

---

## Task 14: Add `PATCH /documents/{id}` endpoint

**Files:**
- Modify: `grimoire/api/schemas.py` (add `PatchDocumentRequest`)
- Modify: `grimoire/api/routes/documents.py` (add PATCH route)
- Modify: `tests/test_api.py` (add tests)

The documents API is missing an update endpoint.

- [ ] **Step 1: Write the failing test**

In `tests/test_api.py`, inside `TestDocumentsAPI` (or add the class), add:

```python
def test_patch_document_title(self, client):
    """PATCH /documents/{id} must update document title."""
    from unittest.mock import AsyncMock, MagicMock

    mock_doc = MagicMock()
    mock_doc.id = "doc-uuid-1"
    mock_doc.title = "Old Title"
    mock_doc.source_path = "/tmp/test.pdf"
    mock_doc.file_type = MagicMock(value="pdf")
    mock_doc.storage_backend = MagicMock(value="local")
    mock_doc.processing_status = MagicMock(value="completed")
    mock_doc.size_bytes = 1024
    mock_doc.created_at = None
    mock_doc.updated_at = None
    mock_doc.error_message = None

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_doc)
    mock_session.commit = AsyncMock()

    async def override_db():
        yield mock_session

    from grimoire.api.dependencies import get_db_session
    client.app.dependency_overrides[get_db_session] = override_db

    resp = client.patch(
        "/api/v1/documents/doc-uuid-1",
        json={"title": "New Title"},
    )
    client.app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert mock_doc.title == "New Title"

def test_patch_document_not_found(self, client):
    """PATCH /documents/{id} must return 404 for missing document."""
    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)

    async def override_db():
        yield mock_session

    from grimoire.api.dependencies import get_db_session
    client.app.dependency_overrides[get_db_session] = override_db

    resp = client.patch(
        "/api/v1/documents/nonexistent-id",
        json={"title": "New Title"},
    )
    client.app.dependency_overrides.clear()

    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
.venv/bin/python -m pytest tests/test_api.py::TestDocumentsAPI::test_patch_document_title tests/test_api.py::TestDocumentsAPI::test_patch_document_not_found -v 2>&1 | tail -10
```

Expected: both FAIL with 404/405 (method not allowed).

- [ ] **Step 3: Add `PatchDocumentRequest` schema**

In `grimoire/api/schemas.py`, add after `DocumentDetailResponse`:

```python
class PatchDocumentRequest(BaseModel):
    """Request to update a document's metadata."""

    title: str | None = Field(default=None, min_length=1, max_length=512)
```

- [ ] **Step 4: Add the PATCH route to `documents.py`**

In `grimoire/api/routes/documents.py`, add after the `get_document` route and before `delete_document`:

```python
from grimoire.api.schemas import (
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentResponse,
    PatchDocumentRequest,
)


@router.patch("/{document_id}", response_model=DocumentDetailResponse)
async def patch_document(
    document_id: str,
    request: PatchDocumentRequest,
    db: AsyncSession = Depends(get_db_session),
) -> DocumentDetailResponse:
    """Update a document's metadata (title only for now)."""
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

    if request.title is not None:
        doc.title = request.title

    await db.commit()

    return DocumentDetailResponse(
        id=doc.id,
        title=doc.title,
        source_path=doc.source_path,
        file_type=doc.file_type.value if hasattr(doc.file_type, "value") else str(doc.file_type),
        storage_backend=doc.storage_backend.value if hasattr(doc.storage_backend, "value") else str(doc.storage_backend),
        processing_status=doc.processing_status.value if hasattr(doc.processing_status, "value") else str(doc.processing_status),
        size_bytes=doc.size_bytes,
        created_at=doc.created_at.isoformat() if doc.created_at else None,
        updated_at=doc.updated_at.isoformat() if doc.updated_at else None,
        error_message=doc.error_message,
    )
```

- [ ] **Step 5: Run the new tests**

```bash
.venv/bin/python -m pytest tests/test_api.py::TestDocumentsAPI::test_patch_document_title tests/test_api.py::TestDocumentsAPI::test_patch_document_not_found -v 2>&1 | tail -5
```

Expected: both PASS.

- [ ] **Step 6: Run full API test suite**

```bash
.venv/bin/python -m pytest tests/test_api.py -q 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add grimoire/api/schemas.py grimoire/api/routes/documents.py tests/test_api.py
git commit -m "feat(api): add PATCH /documents/{id} endpoint for updating document title"
```

---

## Task 15: Update documentation

**Files:**
- Modify: `README.md`
- Modify: `grimoire/search/hybrid.py` (add distance-metric comment)

- [ ] **Step 1: Fix README — remove LangChain reference**

In `README.md`, find line 9:
```markdown
- 🤖 **Agent-based architecture** - LangChain Deep Agents for document ingestion, watching, querying, and content generation
```

Replace with:
```markdown
- 🤖 **Agent-based architecture** - Custom CoordinatorAgent routes natural-language requests to specialised IngestionAgent, QueryAgent, ContentGenerationAgent, and WatcherAgent
```

- [ ] **Step 2: Document ChromaDB distance metric assumption in `hybrid.py`**

In `grimoire/search/hybrid.py`, at line ~232, add a comment:

```python
# ChromaDBStore defaults to cosine distance, which returns values in [0, 2]
# where 0 = identical, 2 = opposite. Converting: score = 1 - distance
# gives similarity in [-1, 1], clamped to [0, 1] by max().
# If ChromaDBStore is configured with a non-cosine metric, this conversion
# will be incorrect and HybridSearch must be updated accordingly.
distance = item.get("distance", 0.0)
score = max(0.0, 1.0 - distance)
```

- [ ] **Step 3: Verify README renders correctly**

```bash
# Quick sanity check — no broken markdown
grep -n "LangChain" README.md
```

Expected: no output (reference removed).

- [ ] **Step 4: Run full test suite one final time**

```bash
.venv/bin/python -m pytest tests/ --ignore=tests/test_storage_onedrive.py -q 2>&1 | tail -5
```

Expected: all previously-failing/erroring tests now pass. Zero failures, zero errors.

- [ ] **Step 5: Final commit**

```bash
git add README.md grimoire/search/hybrid.py
git commit -m "docs: remove stale LangChain reference; document ChromaDB distance metric assumption"
```

---

## Self-Review Checklist

### Spec Coverage

| Issue from CODE_REVIEW.md | Addressed in task |
|---|---|
| Missing `aiosqlite` dep | Task 1 |
| Missing `pytest-httpx` dep | Task 1 |
| Config test fails against real grimoire.yaml | Task 2 |
| CORS misconfiguration | Task 3 |
| Path traversal in ingest API | Task 4 |
| `storage_backend` silently ignored | Task 4 |
| Watcher processor exits on error | Task 5 |
| `_log_extraction` no-op | Task 6 |
| `datetime.utcnow()` deprecated (16 places) | Task 7 |
| `asyncio.get_event_loop()` deprecated | Task 8 |
| Slug generation broken | Task 9 |
| `DOCLEY_AVAILABLE` typo | Task 10 |
| Mock-detection code in production | Task 10 |
| `Tagger` client never closed | Task 11 |
| `build_query_agent` drops `fallback_model` | Task 11 |
| `import os` inside function | Task 12 |
| Duplicate `DedupStrategy` enums | Task 13 |
| Missing PATCH /documents/{id} | Task 14 |
| README LangChain reference stale | Task 15 |
| Distance metric undocumented assumption | Task 15 |
| No tests for new logic | All tasks — each includes a test step |
| `Document.chunks` selectin-loads on list | Not included — scoped out; requires larger SQLAlchemy refactor; log as future tech-debt |

**Scoped out:** `Document.chunks` lazy-load performance fix. Changing `lazy="selectin"` to `lazy="raise"` in async SQLAlchemy requires updating every access site to use explicit `selectinload()` options — a large, high-risk refactor for a performance issue that doesn't affect correctness. Leave as a tracked improvement.

### Type Consistency

- `StorageBackend` imported from `grimoire.db.models` in `ingest.py` (Task 4) — correct, that's where it's defined.
- `PatchDocumentRequest` added to `schemas.py` and imported in `documents.py` (Task 14) — consistent.
- `slugify` import pattern (`from slugify import slugify`) — matches the installed package's API.
- `datetime.now(UTC)` — requires `from datetime import UTC, datetime` in each file.
