"""``GET /categories`` reports how many documents carry each category.

Real SQLite, not mocks: the point is that each count belongs to its own
category and is computed in one query, which a mocked session cannot show.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from grimoire.api.auth import get_api_key
from grimoire.api.dependencies import get_db_session
from grimoire.api.main import create_app
from grimoire.db.models import (
    ApiKey,
    ApiKeyTier,
    Base,
    Category,
    Document,
    DocumentTag,
    FileType,
    ProcessingStatus,
    StorageBackend,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ALPHA = "b0000000-0000-4000-8000-00000000000a"
BETA = "b0000000-0000-4000-8000-00000000000b"
CHILD = "b0000000-0000-4000-8000-00000000000c"


def _doc(n: int) -> Document:
    # A hex letter in the id: SQLite stores UUIDs with NUMERIC affinity.
    return Document(
        id=f"a0000000-0000-4000-8000-00000000000{n}",
        source_path=f"/docs/{n}.md",
        storage_backend=StorageBackend.LOCAL,
        file_type=FileType.MD,
        file_hash=f"{n:064x}",
        title=f"Doc {n}",
        size_bytes=1,
        processing_status=ProcessingStatus.COMPLETED,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def sql_log() -> list[str]:
    return []


@pytest.fixture
def client(tmp_path: Path, sql_log: list[str]) -> Iterator[TestClient]:
    path = tmp_path / "cats.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all(
            [
                Category(id=ALPHA, name="Alpha", slug="alpha"),
                Category(id=BETA, name="Beta", slug="beta"),
                Category(
                    id=CHILD, name="Alpha Child", slug="alpha-child", parent_id=ALPHA
                ),
            ]
        )
        s.add_all([_doc(1), _doc(2), _doc(3)])
        s.flush()
        # Alpha: docs 1 and 2.  Beta: none.  Alpha Child: doc 3 only.
        s.add_all(
            [
                DocumentTag(document_id=_doc(1).id, category_id=ALPHA),
                DocumentTag(document_id=_doc(2).id, category_id=ALPHA),
                DocumentTag(document_id=_doc(3).id, category_id=CHILD),
            ]
        )
        s.commit()
    engine.dispose()

    app = create_app(use_lifespan=False)
    app.state.limiter.enabled = False
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(async_engine.sync_engine, "before_cursor_execute")
    def _record(conn: Any, cursor: Any, statement: str, *_a: Any) -> None:
        sql_log.append(statement)

    maker = async_sessionmaker(async_engine, expire_on_commit=False)

    async def override_db() -> AsyncIterator[Any]:
        async with maker() as session:
            yield session

    async def override_key() -> ApiKey:
        return ApiKey(
            id="k",
            name="k",
            tier=ApiKeyTier.AGENT,
            key_prefix="grim_agt_tst",
            key_hash="x",
            created_at=NOW,
        )

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_api_key] = override_key
    with TestClient(app) as c:
        yield c


def _counts(client: TestClient) -> dict[str, int]:
    body = client.get("/api/v1/categories").json()
    return {c["slug"]: c["document_count"] for c in body["categories"]}


def test_each_category_reports_its_own_document_count(client: TestClient) -> None:
    assert _counts(client) == {"alpha": 2, "alpha-child": 1, "beta": 0}


def test_a_child_is_not_counted_into_its_parent(client: TestClient) -> None:
    # Alpha's count is documents tagged Alpha, not Alpha plus descendants.
    assert _counts(client)["alpha"] == 2


def test_the_list_stays_sorted_by_name_with_the_right_total(client: TestClient) -> None:
    body = client.get("/api/v1/categories").json()
    assert [c["name"] for c in body["categories"]] == ["Alpha", "Alpha Child", "Beta"]
    assert body["total"] == 3


def test_counts_do_not_cost_a_query_per_category(
    client: TestClient, sql_log: list[str]
) -> None:
    sql_log.clear()
    client.get("/api/v1/categories")
    selects = [s for s in sql_log if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 2  # the page (with counts) and the total


def test_a_new_category_starts_at_zero(client: TestClient) -> None:
    response = client.post("/api/v1/categories", json={"name": "Gamma"})
    assert response.status_code == 201
    assert response.json()["document_count"] == 0
