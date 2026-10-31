"""/readyz: dependency readiness, plus watcher startup status (audit C-9)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def deps() -> Iterator[dict[str, Any]]:
    """Postgres, ChromaDB and Redis all reachable, as mocks a test can break."""
    conn = AsyncMock()

    @asynccontextmanager
    async def _connect() -> Any:
        yield conn

    manager = MagicMock()
    manager._engine.connect = _connect
    store = AsyncMock()
    redis_client = AsyncMock()
    with (
        patch("grimoire.db.session.get_db_manager", return_value=manager),
        patch(
            "grimoire.vectorstore.factory.get_maintenance_store",
            AsyncMock(return_value=store),
        ),
        patch("redis.asyncio.from_url", return_value=redis_client),
    ):
        yield {"conn": conn, "store": store, "redis": redis_client}


def _client() -> TestClient:
    from grimoire.api.main import create_app

    app = create_app(use_lifespan=False)
    return TestClient(app, raise_server_exceptions=False)


def test_ready_when_dependencies_answer(deps: dict[str, Any]) -> None:
    resp = _client().get("/readyz")
    assert resp.status_code == 200, resp.text
    checks = resp.json()["checks"]
    assert checks["postgres"] == checks["chromadb"] == checks["redis"] == "ok"
    assert checks["watcher"] == "disabled"  # no lifespan ran


def test_chromadb_down_is_not_ready(deps: dict[str, Any]) -> None:
    deps["store"].count = AsyncMock(side_effect=ConnectionError("refused"))
    resp = _client().get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["detail"]["checks"]["chromadb"] != "ok"


def test_failed_watcher_is_reported_but_still_ready(deps: dict[str, Any]) -> None:
    client = _client()
    client.app.state.watcher_status = "error"  # type: ignore[attr-defined]
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"]["watcher"] == "error"


async def test_lifespan_records_watcher_startup_failure() -> None:
    from fastapi import FastAPI

    from grimoire.api.main import lifespan

    app = FastAPI()
    with (
        patch("grimoire.db.session.initialize_db", AsyncMock()),
        patch("grimoire.db.session.close_db", AsyncMock()),
        patch("grimoire.db.session.get_db_manager", return_value=MagicMock()),
        patch(
            "grimoire.agents.watcher.WatcherAgent",
            side_effect=RuntimeError("no inotify"),
        ),
    ):
        async with lifespan(app):
            assert app.state.watcher_status == "error"
