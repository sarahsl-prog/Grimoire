"""``tag_count`` / ``chunk_count`` / ``tags`` on the documents endpoints (A1).

These run against a real SQLite database, not mocks: the point is that the
counts are right and are computed per document, which a mocked session cannot
show.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
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
    Chunk,
    Document,
    DocumentTag,
    FileType,
    ProcessingStatus,
    StorageBackend,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _doc(n: int, status: ProcessingStatus = ProcessingStatus.COMPLETED) -> Document:
    # The id must contain a hex letter: SQLite stores UUID columns with NUMERIC
    # affinity, so an all-digit id would be read back as an integer.
    return Document(
        id=f"a0000000-0000-4000-8000-00000000000{n}",
        source_path=f"/docs/{n}.md",
        storage_backend=StorageBackend.LOCAL,
        file_type=FileType.MD,
        file_hash=f"{n:064x}",
        title=f"Doc {n}",
        size_bytes=10,
        processing_status=status,
        # Newest first in the listing is doc 1, then 2, then 3.
        created_at=NOW - timedelta(minutes=n),
        updated_at=NOW,
    )


def _chunks(doc_id: str, count: int) -> list[Chunk]:
    return [
        Chunk(
            document_id=doc_id,
            chunk_index=i,
            content=f"chunk {i}",
            token_count=2,
        )
        for i in range(count)
    ]


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """A seeded SQLite file.

    Doc 1: 2 chunks, 2 tags.  Doc 2: none.  Doc 3: 3 chunks, 1 tag (failed).
    A file rather than ``:memory:`` so the app's own async engine, created
    inside the TestClient's event loop, sees what this sync engine wrote.
    """
    path = tmp_path / "counts.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        d1, d2, d3 = _doc(1), _doc(2), _doc(3, ProcessingStatus.FAILED)
        s.add_all([d1, d2, d3])
        s.flush()
        s.add_all(_chunks(d1.id, 2) + _chunks(d3.id, 3))
        alpha = Category(name="Alpha", slug="alpha")
        beta = Category(name="Beta", slug="beta")
        s.add_all([alpha, beta])
        s.flush()
        s.add_all(
            [
                DocumentTag(document_id=d1.id, category_id=beta.id),
                DocumentTag(document_id=d1.id, category_id=alpha.id),
                DocumentTag(document_id=d3.id, category_id=beta.id),
            ]
        )
        s.commit()
    engine.dispose()
    return f"sqlite+aiosqlite:///{path}"


@pytest.fixture
def sql_log() -> list[str]:
    """Every SQL statement the app's engine executes, in order."""
    return []


@pytest.fixture
def client(db_url: str, sql_log: list[str]) -> Iterator[TestClient]:
    app = create_app(use_lifespan=False)
    # Rate limiting is covered in test_security.py; here it would need Redis.
    app.state.limiter.enabled = False
    engine = create_async_engine(db_url)

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(conn: Any, cursor: Any, statement: str, *_a: Any) -> None:
        sql_log.append(statement)

    maker = async_sessionmaker(engine, expire_on_commit=False)

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


def _by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["id"][-1]: d for d in body["documents"]}


class TestList:
    def test_counts_are_per_document(self, client: TestClient) -> None:
        docs = _by_id(client.get("/api/v1/documents").json())
        assert {k: (d["chunk_count"], d["tag_count"]) for k, d in docs.items()} == {
            "1": (2, 2),
            "2": (0, 0),
            "3": (3, 1),
        }

    def test_a_document_with_neither_reports_zero_not_missing(
        self, client: TestClient
    ) -> None:
        doc = _by_id(client.get("/api/v1/documents").json())["2"]
        assert doc["chunk_count"] == 0
        assert doc["tag_count"] == 0

    def test_counts_follow_the_page_not_the_whole_table(
        self, client: TestClient
    ) -> None:
        body = client.get("/api/v1/documents", params={"limit": 1, "offset": 2}).json()
        assert body["total"] == 3
        assert [
            (d["id"][-1], d["chunk_count"], d["tag_count"]) for d in body["documents"]
        ] == [("3", 3, 1)]

    def test_counts_survive_filters(self, client: TestClient) -> None:
        body = client.get("/api/v1/documents", params={"status": "failed"}).json()
        assert [
            (d["id"][-1], d["chunk_count"], d["tag_count"]) for d in body["documents"]
        ] == [("3", 3, 1)]
        assert body["total"] == 1

    def test_the_list_does_not_carry_tag_names(self, client: TestClient) -> None:
        """Names are detail-only; the list schema has no ``tags`` field."""
        doc = client.get("/api/v1/documents").json()["documents"][0]
        assert "tags" not in doc


class TestListIsCheap:
    """The counts replace loading the rows they count."""

    def test_listing_does_not_load_chunk_text(
        self, client: TestClient, sql_log: list[str]
    ) -> None:
        client.get("/api/v1/documents")
        assert sql_log, "the probe saw no SQL"
        # Chunk rows (and their text) are only ever needed for the count, which
        # is computed in SQL; a statement selecting chunks.content means every
        # listed document's chunks were pulled into memory.
        assert not [q for q in sql_log if "chunks.content" in q]

    def test_listing_uses_a_fixed_number_of_statements(
        self, client: TestClient, sql_log: list[str]
    ) -> None:
        client.get("/api/v1/documents")
        assert len(sql_log) == 2  # the total, and the page with its counts


class TestDetail:
    def test_tags_are_the_category_names_sorted(self, client: TestClient) -> None:
        body = client.get(
            "/api/v1/documents/a0000000-0000-4000-8000-000000000001"
        ).json()
        assert body["tags"] == ["Alpha", "Beta"]
        assert (body["tag_count"], body["chunk_count"]) == (2, 2)

    def test_a_document_without_tags_or_chunks(self, client: TestClient) -> None:
        body = client.get(
            "/api/v1/documents/a0000000-0000-4000-8000-000000000002"
        ).json()
        assert body["tags"] == []
        assert (body["tag_count"], body["chunk_count"]) == (0, 0)

    def test_another_documents_tags_and_chunks_do_not_bleed_in(
        self, client: TestClient
    ) -> None:
        body = client.get(
            "/api/v1/documents/a0000000-0000-4000-8000-000000000003"
        ).json()
        assert body["tags"] == ["Beta"]
        assert (body["tag_count"], body["chunk_count"]) == (1, 3)

    def test_unknown_document_is_still_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/documents/nope").status_code == 404
