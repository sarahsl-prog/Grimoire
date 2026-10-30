"""Malformed IDs in the URL are 404s, not 500s.

Document and category primary keys are Postgres ``UUID`` columns. Before
``get_by_uuid``, a path like ``/documents/nonexistent-id`` went straight to
``db.get`` and asyncpg rejected the value, so the client got a 500. The
integration suite caught it against a real database; these tests pin it with a
session that fails the same way whenever a non-UUID reaches it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grimoire.api.auth import get_api_key
from grimoire.api.dependencies import get_by_uuid, get_db_session
from grimoire.api.main import create_app
from grimoire.db.models import ApiKey, ApiKeyTier, Category, Document

VALID_ID = str(uuid.uuid4())


class _InvalidUUIDError(Exception):
    """Stands in for asyncpg's DataError on a malformed UUID."""


async def _strict_get(model: Any, ident: Any) -> None:
    """Behave like Postgres: reject non-UUID keys, find nothing otherwise."""
    for part in ident if isinstance(ident, tuple) else (ident,):
        try:
            uuid.UUID(str(part))
        except ValueError as exc:
            raise _InvalidUUIDError(
                f"invalid input syntax for type uuid: {part!r}"
            ) from exc
    return None


@pytest.fixture
def session() -> AsyncMock:
    db = AsyncMock()
    db.get = AsyncMock(side_effect=_strict_get)
    return db


@pytest.fixture
def client(session: AsyncMock) -> Iterator[TestClient]:
    app: FastAPI = create_app(use_lifespan=False)
    if getattr(app.state, "limiter", None):
        app.state.limiter.enabled = False

    async def override_db() -> Any:
        yield session

    async def override_key() -> ApiKey:
        return ApiKey(
            id="test-key-12345678",
            name="test-key",
            tier=ApiKeyTier.AGENT,
            key_prefix="grim_agt_tst",
            key_hash="$2b$12$fakehash",
            created_at=datetime.now(UTC),
        )

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_api_key] = override_key
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# get_by_uuid
# ---------------------------------------------------------------------------


class TestGetByUuid:
    @pytest.mark.parametrize("raw", ["nonexistent-id", "", "123", "../etc/passwd"])
    async def test_malformed_id_is_none_without_touching_the_db(
        self, session: AsyncMock, raw: str
    ) -> None:
        assert await get_by_uuid(session, Document, raw) is None
        session.get.assert_not_called()

    async def test_valid_id_is_looked_up(self, session: AsyncMock) -> None:
        row = object()
        session.get = AsyncMock(return_value=row)
        assert await get_by_uuid(session, Category, VALID_ID) is row
        session.get.assert_awaited_once_with(Category, VALID_ID)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


BAD = "nonexistent-id"


class TestRoutesReturn404ForMalformedIds:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", f"/api/v1/documents/{BAD}"),
            ("DELETE", f"/api/v1/documents/{BAD}"),
            ("DELETE", f"/api/v1/categories/{BAD}"),
            ("PUT", f"/api/v1/documents/{BAD}/tags/{VALID_ID}"),
            ("PUT", f"/api/v1/documents/{VALID_ID}/tags/{BAD}"),
            ("DELETE", f"/api/v1/documents/{BAD}/tags/{VALID_ID}"),
            ("DELETE", f"/api/v1/documents/{VALID_ID}/tags/{BAD}"),
        ],
    )
    def test_malformed_id(self, client: TestClient, method: str, path: str) -> None:
        resp = client.request(method, path)
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    def test_unknown_valid_id_still_404(self, client: TestClient) -> None:
        assert client.get(f"/api/v1/documents/{VALID_ID}").status_code == 404
