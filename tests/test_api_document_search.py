"""Free-text search on ``GET /documents`` via ``q`` (D: document search).

Runs against a real SQLite database, like the document-count tests: whether
``LIKE`` wildcards are escaped and whether the total follows the filter is
exactly what a mocked session cannot show.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from grimoire.api.auth import get_api_key
from grimoire.api.dependencies import get_db_session
from grimoire.api.main import create_app
from grimoire.db.models import (
    ApiKey,
    ApiKeyTier,
    Base,
    Document,
    FileType,
    ProcessingStatus,
    StorageBackend,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)

# (title, source_path, status); listed newest first, so doc 1 is the newest.
SEED = [
    ("Kubernetes Hardening Guide", "/docs/k8s-hardening.md", "completed"),
    ("Postgres tuning notes", "/docs/pg.md", "completed"),
    ("KUBERNETES cheat sheet", "/docs/cheats/k8s.md", "failed"),
    ("100%_literal title", "/docs/literal.md", "completed"),
    ("Quarterly report", "/finance/q3-budget-notes.md", "completed"),
]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    path = tmp_path / "search.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for n, (title, source, status) in enumerate(SEED, start=1):
            s.add(
                Document(
                    # Needs a hex letter: SQLite stores UUIDs with NUMERIC affinity.
                    id=f"a0000000-0000-4000-8000-00000000000{n}",
                    source_path=source,
                    storage_backend=StorageBackend.LOCAL,
                    file_type=FileType.MD,
                    file_hash=f"{n:064x}",
                    title=title,
                    size_bytes=10,
                    processing_status=ProcessingStatus(status),
                    created_at=NOW - timedelta(minutes=n),
                    updated_at=NOW,
                )
            )
        s.commit()
    engine.dispose()

    app = create_app(use_lifespan=False)
    app.state.limiter.enabled = False  # rate limiting is covered elsewhere
    maker = async_sessionmaker(
        create_async_engine(f"sqlite+aiosqlite:///{path}"), expire_on_commit=False
    )

    async def override_db() -> AsyncIterator[Any]:
        async with maker() as session:
            yield session

    async def override_key() -> ApiKey:
        return ApiKey(
            id="k",
            name="k",
            tier=ApiKeyTier.READ,
            key_prefix="grim_rdl_tst",
            key_hash="x",
            created_at=NOW,
        )

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_api_key] = override_key
    with TestClient(app) as c:
        yield c


def _titles(client: TestClient, **params: Any) -> list[str]:
    response = client.get("/api/v1/documents", params=params)
    assert response.status_code == 200, response.text
    return [d["title"] for d in response.json()["documents"]]


def test_matches_a_title_substring_case_insensitively(client: TestClient) -> None:
    assert _titles(client, q="kubernetes") == [
        "Kubernetes Hardening Guide",
        "KUBERNETES cheat sheet",
    ]


def test_matches_the_source_path_as_well(client: TestClient) -> None:
    # "q3-budget" is only in the path of the "Quarterly report" document.
    assert _titles(client, q="q3-budget") == ["Quarterly report"]


def test_no_match_is_an_empty_page_with_total_zero(client: TestClient) -> None:
    body = client.get("/api/v1/documents", params={"q": "zzz-nothing"}).json()
    assert body["documents"] == []
    assert body["total"] == 0


def test_total_counts_matches_not_the_whole_table(client: TestClient) -> None:
    body = client.get("/api/v1/documents", params={"q": "kubernetes"}).json()
    assert body["total"] == 2


def test_composes_with_the_status_filter(client: TestClient) -> None:
    assert _titles(client, q="kubernetes", status="failed") == [
        "KUBERNETES cheat sheet"
    ]


def test_paginates_within_the_matches(client: TestClient) -> None:
    body = client.get(
        "/api/v1/documents", params={"q": "kubernetes", "limit": 1, "offset": 1}
    ).json()
    assert body["total"] == 2
    assert [d["title"] for d in body["documents"]] == ["KUBERNETES cheat sheet"]


@pytest.mark.parametrize("wildcard", ["%", "_"])
def test_like_wildcards_are_literal(client: TestClient, wildcard: str) -> None:
    # Unescaped, "%" would match every document and "_" any single character.
    assert _titles(client, q=wildcard) == ["100%_literal title"]


def test_backslash_is_literal(client: TestClient) -> None:
    assert _titles(client, q="\\") == []


def test_blank_query_is_ignored(client: TestClient) -> None:
    assert len(_titles(client, q="   ")) == len(SEED)


def test_surrounding_whitespace_is_trimmed(client: TestClient) -> None:
    assert _titles(client, q="  postgres  ") == ["Postgres tuning notes"]


def test_overlong_query_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/documents", params={"q": "x" * 201})
    assert response.status_code == 422
