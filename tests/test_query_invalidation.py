"""Cached query answers are dropped whenever documents change.

Audit finding C-7: only the REST delete route and the watcher invalidated,
both through a ``hasattr`` guard that turned wiring mistakes into silent
no-ops; REST and MCP ingest, tagging and the CLI never did, so answers
could be up to a TTL out of date. Invalidation now goes through one helper,
``grimoire.core.cache.invalidate_query_results``, called after the commit.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from grimoire.agents.ingestion import IngestionResult
from grimoire.core.cache import CacheKeyPrefix, DiskCache, invalidate_query_results

DOC_ID = "7d3c1f0e-8a2b-4c5d-9e6f-0123456789ab"
CAT_ID = "1b2c3d4e-5f60-4718-9a0b-c1d2e3f40516"


def _result(status: str) -> IngestionResult:
    return IngestionResult(file_path="/tmp/x.txt", document_id=DOC_ID, status=status)


def _disk_settings(tmp_path: Path) -> Any:
    from grimoire.config.settings import CacheStorage, GrimoireSettings

    base = GrimoireSettings()
    return base.model_copy(
        update={
            "cache": base.cache.model_copy(
                update={"storage": CacheStorage.DISK, "path": tmp_path}
            )
        }
    )


class TestHelper:
    async def test_clears_only_query_results(self, tmp_path: Path) -> None:
        query = DiskCache(path=tmp_path, namespace=CacheKeyPrefix.QUERY)
        embed = DiskCache(path=tmp_path, namespace=CacheKeyPrefix.EMBEDDING)
        await query.set("q", "answer")
        await embed.set("e", [0.1])

        await invalidate_query_results(_disk_settings(tmp_path))

        assert await query.get("q") is None
        assert await embed.get("e") == [0.1]

    async def test_never_raises(self) -> None:
        with patch(
            "grimoire.core.cache.CacheFactory.from_settings",
            side_effect=RuntimeError("redis down"),
        ):
            await invalidate_query_results()  # logs a warning instead


@pytest.fixture
def api() -> Iterator[tuple[Any, AsyncMock, list[str]]]:
    from grimoire.api.auth import get_api_key
    from grimoire.api.dependencies import get_db_session
    from grimoire.api.main import create_app
    from grimoire.db.models import ApiKey, ApiKeyTier

    app = create_app(use_lifespan=False)
    if getattr(app.state, "limiter", None):
        app.state.limiter.enabled = False
    calls: list[str] = []
    session = AsyncMock()
    session.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
    session.add = MagicMock()  # synchronous on a real AsyncSession

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


def _recording(calls: list[str]) -> AsyncMock:
    return AsyncMock(side_effect=lambda *a, **k: calls.append("invalidate"))


class TestRestIngest:
    def _post(
        self, api: tuple[Any, AsyncMock, list[str]], tmp_path: Path, status: str
    ) -> list[str]:
        app, _, calls = api
        f = tmp_path / "doc.txt"
        f.write_text("hello")
        agent = MagicMock()
        agent.ingest_file = AsyncMock(return_value=_result(status))
        with (
            patch("grimoire.api.routes.ingest.get_ingestion_agent", return_value=agent),
            patch(
                "grimoire.api.routes.ingest.invalidate_query_results", _recording(calls)
            ),
            TestClient(app) as c,
        ):
            resp = c.post("/api/v1/ingest/file", json={"file_path": str(f)})
        assert resp.status_code == 200, resp.text
        return calls

    def test_completed_ingest_commits_then_invalidates(
        self, api: tuple[Any, AsyncMock, list[str]], tmp_path: Path
    ) -> None:
        calls = self._post(api, tmp_path, "completed")
        # First "commit" is ours; the session's own commit at teardown follows.
        assert calls[:2] == ["commit", "invalidate"]

    def test_skipped_ingest_does_not_invalidate(
        self, api: tuple[Any, AsyncMock, list[str]], tmp_path: Path
    ) -> None:
        assert "invalidate" not in self._post(api, tmp_path, "skipped")


class TestRestTagging:
    def test_tag_invalidates_after_commit(
        self, api: tuple[Any, AsyncMock, list[str]]
    ) -> None:
        app, _, calls = api
        with (
            patch(
                "grimoire.api.routes.documents._tag_target",
                AsyncMock(return_value=None),
            ),
            patch(
                "grimoire.api.routes.documents.invalidate_query_results",
                _recording(calls),
            ),
            TestClient(app) as c,
        ):
            resp = c.put(f"/api/v1/documents/{DOC_ID}/tags/{CAT_ID}")
        assert resp.status_code == 204, resp.text
        assert calls[:2] == ["commit", "invalidate"]


class TestMcpIngest:
    async def test_completed_ingest_invalidates(self, tmp_path: Path) -> None:
        from grimoire.db.models import ApiKey, ApiKeyTier
        from grimoire.mcp.auth_stdio import set_current_api_key
        from grimoire.mcp.tools import IngestFileInput, grimoire_ingest_file

        set_current_api_key(
            ApiKey(
                id="k",
                name="t",
                tier=ApiKeyTier.DEV,
                key_prefix="grim_dvl_tst",
                key_hash="x",
                created_at=datetime.now(UTC),
            )
        )
        f = tmp_path / "doc.txt"
        f.write_text("hello")
        agent = MagicMock()
        agent.ingest_file = AsyncMock(return_value=_result("completed"))
        invalidate = AsyncMock()

        @asynccontextmanager
        async def _ctx() -> Any:
            yield AsyncMock()

        with (
            patch("grimoire.mcp.tools.get_ingestion_agent", return_value=agent),
            patch("grimoire.mcp.tools.get_db_context", _ctx),
            patch("grimoire.mcp.tools.invalidate_query_results", invalidate),
        ):
            out = await grimoire_ingest_file(
                IngestFileInput(file_path=str(f)), ctx=MagicMock()
            )
        assert '"status": "ok"' in out
        invalidate.assert_awaited_once()


class TestCliIngest:
    def test_completed_ingest_invalidates(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from grimoire.cli.main import cli

        f = tmp_path / "doc.txt"
        f.write_text("hello")
        agent = MagicMock()
        agent.ingest_file = AsyncMock(return_value=_result("completed"))
        invalidate = AsyncMock()

        @asynccontextmanager
        async def _ctx() -> Any:
            yield AsyncMock()

        with (
            patch("grimoire.cli.ingest.build_ingestion_agent", return_value=agent),
            patch("grimoire.cli.ingest.get_db_context", _ctx),
            patch("grimoire.cli.ingest.setup_db", AsyncMock()),
            patch("grimoire.cli.ingest.teardown_db", AsyncMock()),
            patch("grimoire.cli.ingest.invalidate_query_results", invalidate),
        ):
            result = CliRunner().invoke(cli, ["ingest", str(f)])
        assert result.exit_code == 0, result.output
        invalidate.assert_awaited_once()
