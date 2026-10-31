"""One delete contract for REST, MCP and the watcher.

Rows are deleted and committed first; vectors are purged afterwards by
grimoire.vectorstore.cleanup.purge_document_vectors, which never raises.
Before this, the REST route deleted vectors first and returned a raw 500 if
ChromaDB was down, while the MCP tool did the opposite and reported an error
for a delete that had succeeded.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from grimoire.vectorstore.cleanup import purge_document_vectors

DOC_ID = "7d3c1f0e-8a2b-4c5d-9e6f-0123456789ab"
_CLEANUP = "grimoire.vectorstore.cleanup"


@pytest.fixture
def errors() -> Iterator[list[str]]:
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="ERROR")
    yield messages
    logger.remove(sink)


def _doc_with_vectors(*vector_ids: str) -> MagicMock:
    doc = MagicMock()
    doc.id = DOC_ID
    doc.chunks = [MagicMock(vector_id=v) for v in vector_ids]
    return doc


class TestPurgeDocumentVectors:
    async def test_nothing_to_delete_does_not_open_a_store(self) -> None:
        with patch(f"{_CLEANUP}.get_maintenance_store") as get_store:
            assert await purge_document_vectors(DOC_ID, []) is True
        get_store.assert_not_called()

    async def test_deletes_the_given_ids(self) -> None:
        store = AsyncMock()
        with patch(f"{_CLEANUP}.get_maintenance_store", AsyncMock(return_value=store)):
            assert await purge_document_vectors(DOC_ID, ["v1", "v2"]) is True
        store.delete.assert_awaited_once_with(["v1", "v2"])

    async def test_unreachable_store_is_logged_not_raised(
        self, errors: list[str]
    ) -> None:
        down = AsyncMock(side_effect=ConnectionError("chroma down"))
        with patch(f"{_CLEANUP}.get_maintenance_store", down):
            assert await purge_document_vectors(DOC_ID, ["v1"]) is False
        assert any(DOC_ID in m and "v1" in m and "orphaned" in m for m in errors)

    async def test_delete_failure_is_logged_not_raised(self, errors: list[str]) -> None:
        store = AsyncMock()
        store.delete = AsyncMock(side_effect=RuntimeError("boom"))
        with patch(f"{_CLEANUP}.get_maintenance_store", AsyncMock(return_value=store)):
            assert await purge_document_vectors(DOC_ID, ["v1"]) is False
        assert any("RuntimeError" in m for m in errors)

    async def test_long_id_lists_are_truncated_in_logs(self, errors: list[str]) -> None:
        ids = [f"v{i}" for i in range(50)]
        down = AsyncMock(side_effect=ConnectionError("down"))
        with patch(f"{_CLEANUP}.get_maintenance_store", down):
            await purge_document_vectors(DOC_ID, ids)
        line = next(m for m in errors if "orphaned" in m)
        assert "50 orphaned" in line and "and 30 more" in line
        assert "v49" not in line


class TestRestDeleteRoute:
    @pytest.fixture
    def app_and_session(self) -> Iterator[tuple[Any, AsyncMock, list[str]]]:
        from grimoire.api.auth import get_api_key
        from grimoire.api.dependencies import get_db_session
        from grimoire.api.main import create_app
        from grimoire.db.models import ApiKey, ApiKeyTier

        app = create_app(use_lifespan=False)
        if getattr(app.state, "limiter", None):
            app.state.limiter.enabled = False

        calls: list[str] = []
        session = AsyncMock()
        session.get = AsyncMock(return_value=_doc_with_vectors("v1", "v2"))
        session.commit = AsyncMock(side_effect=lambda: calls.append("commit"))

        async def _db() -> Any:
            yield session

        key = ApiKey(
            id="k",
            name="t",
            tier=ApiKeyTier.AGENT,
            key_prefix="grim_agt_tst",
            key_hash="x",
            created_at=datetime.now(UTC),
        )

        async def _key() -> ApiKey:
            return key

        app.dependency_overrides[get_db_session] = _db
        app.dependency_overrides[get_api_key] = _key
        yield app, session, calls
        app.dependency_overrides.clear()

    def test_commits_before_purging(
        self, app_and_session: tuple[Any, AsyncMock, list[str]]
    ) -> None:
        app, _, calls = app_and_session

        async def _purge(doc_id: str, ids: list[str]) -> bool:
            calls.append(f"purge {doc_id} {sorted(ids)}")
            return True

        with (
            patch(f"{_CLEANUP}.purge_document_vectors", _purge),
            TestClient(app) as c,
        ):
            resp = c.delete(f"/api/v1/documents/{DOC_ID}")
        assert resp.status_code == 204
        assert calls == ["commit", f"purge {DOC_ID} ['v1', 'v2']"]

    def test_vector_failure_still_returns_204(
        self, app_and_session: tuple[Any, AsyncMock, list[str]]
    ) -> None:
        """The document is gone once committed; orphans are hidden from search."""
        app, _, _ = app_and_session
        with (
            patch(f"{_CLEANUP}.purge_document_vectors", AsyncMock(return_value=False)),
            TestClient(app) as c,
        ):
            assert c.delete(f"/api/v1/documents/{DOC_ID}").status_code == 204

    def test_commit_failure_skips_purge(
        self, app_and_session: tuple[Any, AsyncMock, list[str]]
    ) -> None:
        app, session, _ = app_and_session
        session.commit = AsyncMock(side_effect=RuntimeError("db down"))
        purge = AsyncMock(return_value=True)
        with (
            patch(f"{_CLEANUP}.purge_document_vectors", purge),
            TestClient(app, raise_server_exceptions=False) as c,
        ):
            resp = c.delete(f"/api/v1/documents/{DOC_ID}")
        assert resp.status_code == 500
        purge.assert_not_awaited()  # rows survived, so the vectors must too


class TestMcpDeleteTool:
    async def _call(self, session: AsyncMock, purge: Any) -> str:
        from grimoire.db.models import ApiKey, ApiKeyTier
        from grimoire.mcp.auth_stdio import set_current_api_key
        from grimoire.mcp.tools import DeleteDocumentInput, grimoire_delete_document

        set_current_api_key(
            ApiKey(
                id="k",
                name="t",
                tier=ApiKeyTier.AGENT,
                key_prefix="grim_agt_tst",
                key_hash="x",
                created_at=datetime.now(UTC),
            )
        )

        @asynccontextmanager
        async def _ctx() -> Any:
            yield session

        with (
            patch("grimoire.mcp.tools.get_db_context", _ctx),
            patch(f"{_CLEANUP}.purge_document_vectors", purge),
        ):
            return await grimoire_delete_document(
                DeleteDocumentInput(document_id=DOC_ID), ctx=MagicMock()
            )

    def _session(self) -> AsyncMock:
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = _doc_with_vectors("v1")
        session.execute = AsyncMock(return_value=result)
        return session

    async def test_cleanup_failure_is_reported_but_not_an_error(self) -> None:
        out = await self._call(self._session(), AsyncMock(return_value=False))
        assert '"status": "ok"' in out
        assert '"vectors_removed": false' in out

    async def test_success_reports_vectors_removed(self) -> None:
        out = await self._call(self._session(), AsyncMock(return_value=True))
        assert '"vectors_removed": true' in out

    async def test_commit_failure_skips_purge(self) -> None:
        session = self._session()
        session.commit = AsyncMock(side_effect=RuntimeError("db down"))
        purge = AsyncMock(return_value=True)
        out = await self._call(session, purge)
        assert '"status": "error"' in out
        purge.assert_not_awaited()


class TestWatcherDelete:
    async def test_commits_before_purging(self, tmp_path: Any) -> None:
        from grimoire.agents.watcher import WatcherAgent, _WatchTracker
        from grimoire.storage.base import FileChange, FileChangeType

        calls: list[str] = []
        session = AsyncMock()
        session.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        watcher = WatcherAgent(
            watch_manager=MagicMock(),
            ingestion_agent=MagicMock(),
            db_session_factory=MagicMock(return_value=cm),
        )
        watcher._trackers["w"] = _WatchTracker("w", str(tmp_path), "local")
        repo = MagicMock()
        repo.get_by_source_path = AsyncMock(return_value=_doc_with_vectors("v1"))

        async def _purge(doc_id: str, ids: list[str]) -> bool:
            calls.append(f"purge {doc_id} {ids}")
            return True

        with (
            patch("grimoire.agents.watcher.DocumentRepository", return_value=repo),
            patch("grimoire.agents.watcher.purge_document_vectors", _purge),
            patch("grimoire.agents.watcher.invalidate_query_results", AsyncMock()),
        ):
            await watcher._handle_file_event(
                "w",
                FileChange(
                    change_type=FileChangeType.DELETED,
                    path=str(tmp_path / "gone.pdf"),
                ),
            )
        assert calls == ["commit", f"purge {DOC_ID} ['v1']"]
