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

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from grimoire.api.main import create_app
from grimoire.db.models import ApiKey, ApiKeyTier

_DEPS = "grimoire.api.dependencies"
_ROUTES_MAIN = "grimoire.api.main"
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
        created_at=datetime.now(UTC),
    )
    return key


def _upload_dir_patch_target() -> str:
    """Dotted path of the staging-dir helper, patched to a tmp_path in tests."""
    return f"{_ROUTES_INGEST}._staging_dir"


@pytest.fixture
def app():
    """Create a test FastAPI application (skip lifespan DB init)."""
    test_app = create_app(use_lifespan=False)
    # Disable rate limiting for API endpoint tests; rate-limit logic
    # is exercised separately in test_security.py.
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

    test_key = _make_test_api_key()

    async def override_api_key():
        return test_key

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_api_key] = override_api_key

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    app.dependency_overrides.clear()

    # Wipe rate limit state between tests so /health does not stay throttled
    if hasattr(app.state, "limiter") and app.state.limiter:
        try:
            app.state.limiter.reset()
        except Exception:
            pass
        try:
            storage = app.state.limiter._storage
            storage.reset()
        except Exception:
            pass


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
        import pathlib

        tmp_file = pathlib.Path("/tmp/test.pdf")
        tmp_file.write_text("test")
        try:
            mock_agent = MagicMock()
            mock_result = MagicMock()
            mock_result.model_dump.return_value = {
                "file_path": str(tmp_file),
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

            resp = client.post("/api/v1/ingest/file", json={"file_path": str(tmp_file)})
            assert resp.status_code == 200
            data = resp.json()
            assert data["document_id"] == "doc-1"
            assert data["chunks_created"] == 5
        finally:
            tmp_file.unlink(missing_ok=True)

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_ingest_directory(self, mock_get_agent, client):
        import pathlib

        tmp_dir = pathlib.Path("/tmp/grimoire_test_dir")
        tmp_dir.mkdir(exist_ok=True)
        try:
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

            resp = client.post(
                "/api/v1/ingest/directory", json={"directory": str(tmp_dir)}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 3
            assert data["succeeded"] == 2
        finally:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_upload_ingests_file(self, mock_get_agent, client, tmp_path, monkeypatch):
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "file_path": "staged",
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

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("notes.md", b"# hello", "text/markdown")},
            data={"auto_tag": "false"},
        )

        assert resp.status_code == 200
        assert resp.json()["document_id"] == "doc-1"
        staged = list(tmp_path.iterdir())
        assert len(staged) == 1
        assert staged[0].name.endswith("_notes.md")
        assert staged[0].read_bytes() == b"# hello"
        mock_agent.ingest_file.assert_awaited_once()
        assert mock_agent.ingest_file.await_args.kwargs["auto_tag"] is False

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_upload_unlinks_staged_copy_when_agent_result_is_skipped(
        self, mock_get_agent, client, tmp_path, monkeypatch
    ):
        """A skipped (deduplicated) upload must not leak its staged copy.

        Dedup returns without creating a Document row; the pre-existing
        document's source_path already points at the ORIGINAL file, so the
        newly staged copy this request just wrote is referenced by nothing.
        Left alone it would sit on the uploads volume forever, and
        re-dragging the same file is the ordinary case, not an edge case.
        """
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)
        mock_agent = MagicMock()
        mock_result = MagicMock()
        # `.status` must be set explicitly, not just inferred from
        # `model_dump.return_value`: the route reads `result.status`
        # directly (before it ever calls `.model_dump()`) to decide
        # whether to unlink the staged copy.
        mock_result.status = "skipped"
        mock_result.model_dump.return_value = {
            "file_path": "staged",
            "document_id": "doc-existing",
            "status": "skipped",
            "chunks_created": 0,
            "vectors_stored": 0,
            "tags_applied": 0,
            "error_message": None,
            "duration_ms": 5,
        }
        mock_agent.ingest_file = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("dup.md", b"# hello", "text/markdown")},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"
        assert list(tmp_path.iterdir()) == [], "skipped upload must not orphan a file"

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_upload_keeps_staged_copy_when_agent_result_is_completed(
        self, mock_get_agent, client, tmp_path, monkeypatch
    ):
        """A completed result's Document.source_path points at the staged
        file, so - unlike "skipped" - it must be left in place.
        """
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.status = "completed"
        mock_result.model_dump.return_value = {
            "file_path": "staged",
            "document_id": "doc-new",
            "status": "completed",
            "chunks_created": 2,
            "vectors_stored": 2,
            "tags_applied": 0,
            "error_message": None,
            "duration_ms": 5,
        }
        mock_agent.ingest_file = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("new.md", b"# hello", "text/markdown")},
        )

        assert resp.status_code == 200
        assert len(list(tmp_path.iterdir())) == 1, "completed upload stays staged"

    def test_upload_rejects_unsupported_extension(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
        )

        assert resp.status_code == 415
        assert ".exe" in resp.json()["detail"]
        assert list(tmp_path.iterdir()) == []

    def test_upload_rejects_oversized_file(self, client, tmp_path, monkeypatch):
        """A file over the cap must be rejected and its partial write removed.

        `_UPLOAD_CHUNK_BYTES` is patched down to 4 bytes: at the default
        1 MB chunk size, a 4096-byte body is smaller than one chunk, so the
        very first `handle.write` never runs and this test would only prove
        that an empty freshly-created file gets unlinked. Forcing several
        real chunks makes this prove a genuine multi-chunk PARTIAL write
        (some bytes already on disk) is removed, not just an empty file.
        """
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)
        monkeypatch.setattr(f"{_ROUTES_INGEST}._max_upload_bytes", lambda: 8)
        monkeypatch.setattr(f"{_ROUTES_INGEST}._UPLOAD_CHUNK_BYTES", 4)

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("big.txt", b"x" * 4096, "text/plain")},
        )

        assert resp.status_code == 413
        assert list(tmp_path.iterdir()) == [], "partial file must be removed"

    async def test_stream_upload_null_byte_path_returns_500(self, tmp_path):
        """A destination path with an embedded null byte must not crash raw.

        ``Path.open`` (and ``Path.unlink``, used for cleanup) raise
        ``ValueError`` rather than ``OSError`` for a null byte, so this
        exercises ``_stream_upload_to_disk`` directly rather than through the
        ASGI layer: httpx sanitizes a raw NUL in a multipart filename to
        "%00" before the request ever leaves the client, so an end-to-end
        upload would never reach this code path.
        """
        from fastapi import HTTPException

        from grimoire.api.routes.ingest import _stream_upload_to_disk

        class _FakeUpload:
            def __init__(self, data: bytes) -> None:
                self._chunks = [data, b""]

            async def read(self, _size: int) -> bytes:
                return self._chunks.pop(0)

        destination = tmp_path / "a\x00b.txt"

        with pytest.raises(HTTPException) as exc_info:
            # _FakeUpload duck-types UploadFile's async read(); it is not a
            # subclass, so mypy sees a structural mismatch here.
            await _stream_upload_to_disk(
                _FakeUpload(b"hello"),  # type: ignore[arg-type]
                destination,
                1024,
            )

        assert exc_info.value.status_code == 500

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_upload_sanitizes_traversal_filename(
        self, mock_get_agent, client, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "file_path": "staged",
            "document_id": "doc-2",
            "status": "completed",
            "chunks_created": 1,
            "vectors_stored": 1,
            "tags_applied": 0,
            "error_message": None,
            "duration_ms": 10,
        }
        mock_agent.ingest_file = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("../../etc/passwd.txt", b"root", "text/plain")},
        )

        assert resp.status_code == 200
        staged = list(tmp_path.iterdir())
        assert len(staged) == 1
        assert staged[0].parent == tmp_path
        assert staged[0].name.endswith("_passwd.txt")
        assert ".." not in staged[0].name

    def test_content_length_guard_rejects_oversized_declared_body_before_routing(
        self, client, tmp_path, monkeypatch
    ):
        """A declared Content-Length over the cap must be rejected by the
        middleware alone, before FastAPI parses the multipart body at all.

        Only `_upload_cap_with_margin` is patched here - `_staging_dir` and
        `_max_upload_bytes` (ingest.py's own streaming guard) are left at
        their real values, so a 413 here can only come from the new
        Content-Length middleware, not from `_stream_upload_to_disk`.
        """
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)
        monkeypatch.setattr(f"{_ROUTES_MAIN}._upload_cap_with_margin", lambda: 8)

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("big.txt", b"x" * 4096, "text/plain")},
        )

        assert resp.status_code == 413
        assert list(tmp_path.iterdir()) == [], "nothing must be staged"

    def test_content_length_guard_allows_a_normal_small_upload(
        self, client, tmp_path, monkeypatch
    ):
        """The guard must not interfere with an ordinary upload under the cap."""
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)

        with patch(f"{_ROUTES_INGEST}.get_ingestion_agent") as mock_get_agent:
            mock_agent = MagicMock()
            mock_result = MagicMock()
            mock_result.model_dump.return_value = {
                "file_path": "staged",
                "document_id": "doc-3",
                "status": "completed",
                "chunks_created": 1,
                "vectors_stored": 1,
                "tags_applied": 0,
                "error_message": None,
                "duration_ms": 10,
            }
            mock_agent.ingest_file = AsyncMock(return_value=mock_result)
            mock_get_agent.return_value = mock_agent

            resp = client.post(
                "/api/v1/ingest/upload",
                files={"file": ("notes.md", b"# hello", "text/markdown")},
            )

        assert resp.status_code == 200
        assert len(list(tmp_path.iterdir())) == 1

    def test_upload_requires_api_key(self, app, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from grimoire.api.dependencies import get_db_session

        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)

        async def override_db():
            yield AsyncMock()

        app.dependency_overrides[get_db_session] = override_db
        try:
            with TestClient(app, raise_server_exceptions=False) as unauth:
                resp = unauth.post(
                    "/api/v1/ingest/upload",
                    files={"file": ("notes.md", b"# hello", "text/markdown")},
                )
            assert resp.status_code == 401
            assert (
                list(tmp_path.iterdir()) == []
            ), "unauthenticated upload must not stage"
        finally:
            app.dependency_overrides.clear()


class TestContentLengthGuard:
    """Unit-level tests for the middleware directly, independent of the app.

    These isolate the property that matters most: an oversized request
    never reaches the wrapped ASGI app at all, i.e. it never reaches
    routing or FastAPI's whole-body multipart parsing. They also pin down
    the pure-ASGI property that motivated the rewrite away from
    `BaseHTTPMiddleware`: a non-upload request is forwarded with its
    original `receive`/`send` untouched, never wrapped.
    """

    @staticmethod
    def _scope(
        headers: dict[str, str],
        path: str = "/api/v1/ingest/upload",
        method: str = "POST",
        scope_type: str = "http",
    ) -> dict:
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return {
            "type": scope_type,
            "method": method,
            "path": path,
            "headers": raw_headers,
        }

    @staticmethod
    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    @staticmethod
    def _capture_send() -> tuple[list[dict], object]:
        messages: list[dict] = []

        async def send(message: dict) -> None:
            messages.append(message)

        return messages, send

    async def test_rejects_and_never_calls_downstream_app_when_over_cap(
        self, monkeypatch
    ):
        from grimoire.api.main import ContentLengthGuard

        monkeypatch.setattr(f"{_ROUTES_MAIN}._upload_cap_with_margin", lambda: 100)
        called = False

        async def downstream(scope, receive, send):
            nonlocal called
            called = True
            raise AssertionError("downstream app must not run for an oversized body")

        guard = ContentLengthGuard(downstream)
        messages, send = self._capture_send()

        await guard(self._scope({"content-length": "999999"}), self._receive, send)

        assert called is False
        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 413

    async def test_calls_downstream_app_when_under_cap(self, monkeypatch):
        from starlette.responses import PlainTextResponse

        from grimoire.api.main import ContentLengthGuard

        monkeypatch.setattr(f"{_ROUTES_MAIN}._upload_cap_with_margin", lambda: 100)

        async def downstream(scope, receive, send):
            await PlainTextResponse("ok")(scope, receive, send)

        guard = ContentLengthGuard(downstream)
        messages, send = self._capture_send()

        await guard(self._scope({"content-length": "10"}), self._receive, send)

        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 200

    async def test_missing_content_length_falls_through_to_downstream_app(self):
        from starlette.responses import PlainTextResponse

        from grimoire.api.main import ContentLengthGuard

        async def downstream(scope, receive, send):
            await PlainTextResponse("ok")(scope, receive, send)

        guard = ContentLengthGuard(downstream)
        messages, send = self._capture_send()

        await guard(self._scope({}), self._receive, send)

        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 200

    async def test_unparseable_content_length_falls_through_to_downstream_app(self):
        from starlette.responses import PlainTextResponse

        from grimoire.api.main import ContentLengthGuard

        async def downstream(scope, receive, send):
            await PlainTextResponse("ok")(scope, receive, send)

        guard = ContentLengthGuard(downstream)
        messages, send = self._capture_send()

        await guard(
            self._scope({"content-length": "not-a-number"}), self._receive, send
        )

        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 200

    async def test_other_paths_are_not_capped(self, monkeypatch):
        from starlette.responses import PlainTextResponse

        from grimoire.api.main import ContentLengthGuard

        monkeypatch.setattr(f"{_ROUTES_MAIN}._upload_cap_with_margin", lambda: 100)

        async def downstream(scope, receive, send):
            await PlainTextResponse("ok")(scope, receive, send)

        guard = ContentLengthGuard(downstream)
        messages, send = self._capture_send()

        await guard(
            self._scope({"content-length": "999999"}, path="/api/v1/query/ask"),
            self._receive,
            send,
        )

        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 200

    async def test_non_http_scope_passes_through_untouched(self):
        """A websocket or lifespan scope must never be inspected at all."""
        from grimoire.api.main import ContentLengthGuard

        seen_scope = None

        async def downstream(scope, receive, send):
            nonlocal seen_scope
            seen_scope = scope

        guard = ContentLengthGuard(downstream)
        scope = self._scope({"content-length": "999999"}, scope_type="websocket")

        await guard(scope, self._receive, self._capture_send()[1])

        assert seen_scope is scope

    async def test_non_upload_request_forwards_the_original_receive_and_send(self):
        """The regression this middleware exists to avoid: `BaseHTTPMiddleware`
        wraps every request's `receive`/`send` in its own caching layer, even
        for traffic it does nothing with. A pure-ASGI implementation must
        forward the exact same callables for anything that isn't an upload,
        so a long-lived stream (like the MCP SSE endpoint) is never at risk
        of that wrapping.
        """
        from grimoire.api.main import ContentLengthGuard

        seen_receive = None
        seen_send = None

        async def downstream(scope, receive, send):
            nonlocal seen_receive, seen_send
            seen_receive = receive
            seen_send = send

        guard = ContentLengthGuard(downstream)
        receive = self._receive
        _messages, send = self._capture_send()

        await guard(self._scope({}, path="/health", method="GET"), receive, send)

        assert seen_receive is receive
        assert seen_send is send

    async def test_streaming_response_arrives_incrementally_through_the_guard(self):
        """Regression check for the SSE-buffering concern, at the ASGI level.

        `BaseHTTPMiddleware` collects a streaming response's generator via a
        background task and a memory-object stream before the client sees
        anything, which can break incremental delivery - a real risk for the
        long-lived `/mcp/sse` stream mounted in this same app. A prior
        version of this test tried to observe that through a real HTTP
        client (`httpx.ASGITransport`), but that transport buffers the whole
        response body itself before returning it, so the test deadlocked
        regardless of the guard's behavior - it could not distinguish
        buffering by the middleware from buffering by the transport.

        Driving the guard directly at the ASGI level sidesteps that: the
        downstream app emits an `http.response.body` with `more_body=True`,
        then blocks on an event, then sends the final body message. If
        `ContentLengthGuard` forwarded messages as they are produced (as it
        must, since it delegates to the exact `send` it was given rather
        than collecting anything), the first body message must already be
        recorded before the event is set - not just eventually, but
        strictly before. `asyncio.wait_for` bounds every await so a future
        regression that reintroduces buffering fails loudly instead of
        hanging the suite.
        """
        import asyncio

        from grimoire.api.main import ContentLengthGuard

        release_second_chunk = asyncio.Event()
        first_chunk_seen_before_release: bool | None = None

        async def downstream(scope, receive, send):
            nonlocal first_chunk_seen_before_release
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [[b"content-type", b"text/plain"]],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"first\n",
                    "more_body": True,
                }
            )
            # The guard must have already handed the first chunk to `send`
            # by this point - it is not observing this coroutine's internal
            # state, only what actually reached the recording `send` below.
            first_chunk_seen_before_release = release_second_chunk.is_set()
            await release_second_chunk.wait()
            await send(
                {
                    "type": "http.response.body",
                    "body": b"second\n",
                    "more_body": False,
                }
            )

        guard = ContentLengthGuard(downstream)
        messages: list[dict] = []

        async def send(message: dict) -> None:
            messages.append(message)

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/stream",
            "headers": [],
        }

        guard_task = asyncio.ensure_future(guard(scope, receive, send))

        # Wait until the first body chunk has actually reached `send`,
        # bounded so a regression (the guard withholding it) fails instead
        # of hanging.
        async def wait_for_first_chunk() -> None:
            while not any(
                m.get("type") == "http.response.body" and m.get("body") == b"first\n"
                for m in messages
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_first_chunk(), timeout=5)

        # The first chunk was observed by `send` while the downstream app
        # was still blocked on the event - proving it wasn't withheld until
        # the whole response finished.
        assert first_chunk_seen_before_release is False

        release_second_chunk.set()
        await asyncio.wait_for(guard_task, timeout=5)

        bodies = [m["body"] for m in messages if m.get("type") == "http.response.body"]
        assert bodies == [b"first\n", b"second\n"]


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
