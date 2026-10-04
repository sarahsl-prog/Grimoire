"""Assigning and removing a tag (category) on a document over REST.

Until now a document's tags came only from auto-tagging at ingest. These
endpoints let a person correct them: ``PUT`` and ``DELETE`` on
``/documents/{id}/tags/{category_id}``, at the dev tier.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
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
    TaggedBy,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
DOC = "a0000000-0000-4000-8000-00000000000a"
OTHER_DOC = "a0000000-0000-4000-8000-00000000000b"
ALPHA = "b0000000-0000-4000-8000-00000000000a"
BETA = "b0000000-0000-4000-8000-00000000000b"
MISSING = "c0000000-0000-4000-8000-00000000000c"


def _doc(doc_id: str, n: int) -> Document:
    return Document(
        id=doc_id,
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


class Env:
    def __init__(self, client: TestClient, db_path: Path) -> None:
        self.client = client
        self._path = db_path

    def tags_of(self, doc_id: str) -> list[tuple[str, TaggedBy, float]]:
        engine = create_engine(f"sqlite:///{self._path}")
        with Session(engine) as s:
            rows = s.execute(
                select(
                    DocumentTag.category_id,
                    DocumentTag.tagged_by,
                    DocumentTag.confidence,
                ).where(DocumentTag.document_id == doc_id)
            ).all()
        engine.dispose()
        return [(c, t, f) for c, t, f in rows]


@contextmanager
def _build(tmp_path: Path, tier: ApiKeyTier) -> Iterator[Env]:
    path = tmp_path / "tags.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all(
            [
                Category(id=ALPHA, name="Alpha", slug="alpha"),
                Category(id=BETA, name="Beta", slug="beta"),
                _doc(DOC, 1),
                _doc(OTHER_DOC, 2),
            ]
        )
        s.flush()
        # The other document already carries Beta, set by the LLM.
        s.add(
            DocumentTag(document_id=OTHER_DOC, category_id=BETA, tagged_by=TaggedBy.LLM)
        )
        s.commit()
    engine.dispose()

    app = create_app(use_lifespan=False)
    app.state.limiter.enabled = False
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
            tier=tier,
            key_prefix=f"grim_{tier.value}_tst",
            key_hash="x",
            created_at=NOW,
        )

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_api_key] = override_key
    with TestClient(app) as c:
        yield Env(c, path)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    with _build(tmp_path, ApiKeyTier.DEV) as e:
        yield e


def _url(doc_id: str, category_id: str) -> str:
    return f"/api/v1/documents/{doc_id}/tags/{category_id}"


class TestAssign:
    def test_adds_the_tag_marked_as_the_users_own(self, env: Env) -> None:
        response = env.client.put(_url(DOC, ALPHA))

        assert response.status_code == 204
        assert env.tags_of(DOC) == [(ALPHA, TaggedBy.USER, 1.0)]

    def test_is_idempotent(self, env: Env) -> None:
        env.client.put(_url(DOC, ALPHA))
        again = env.client.put(_url(DOC, ALPHA))

        assert again.status_code == 204
        assert len(env.tags_of(DOC)) == 1

    def test_does_not_rewrite_a_tag_that_already_exists(self, env: Env) -> None:
        # OTHER_DOC's Beta tag came from the LLM; asking again leaves it alone.
        assert env.client.put(_url(OTHER_DOC, BETA)).status_code == 204
        assert env.tags_of(OTHER_DOC) == [(BETA, TaggedBy.LLM, 1.0)]

    def test_tags_one_document_without_touching_another(self, env: Env) -> None:
        env.client.put(_url(DOC, ALPHA))
        assert env.tags_of(OTHER_DOC) == [(BETA, TaggedBy.LLM, 1.0)]

    @pytest.mark.parametrize(
        ("doc_id", "category_id"), [(MISSING, ALPHA), (DOC, MISSING)]
    )
    def test_unknown_document_or_category_is_404(
        self, env: Env, doc_id: str, category_id: str
    ) -> None:
        assert env.client.put(_url(doc_id, category_id)).status_code == 404
        assert env.tags_of(DOC) == []


class TestRemove:
    def test_removes_the_tag(self, env: Env) -> None:
        env.client.put(_url(DOC, ALPHA))
        response = env.client.delete(_url(DOC, ALPHA))

        assert response.status_code == 204
        assert env.tags_of(DOC) == []

    def test_removes_an_llm_assigned_tag_too(self, env: Env) -> None:
        assert env.client.delete(_url(OTHER_DOC, BETA)).status_code == 204
        assert env.tags_of(OTHER_DOC) == []

    def test_removing_a_tag_that_is_not_there_is_not_an_error(self, env: Env) -> None:
        assert env.client.delete(_url(DOC, ALPHA)).status_code == 204

    def test_only_the_named_tag_goes(self, env: Env) -> None:
        env.client.put(_url(DOC, ALPHA))
        env.client.put(_url(DOC, BETA))
        env.client.delete(_url(DOC, ALPHA))

        assert [c for c, _, _ in env.tags_of(DOC)] == [BETA]

    @pytest.mark.parametrize(
        ("doc_id", "category_id"), [(MISSING, ALPHA), (DOC, MISSING)]
    )
    def test_unknown_document_or_category_is_404(
        self, env: Env, doc_id: str, category_id: str
    ) -> None:
        assert env.client.delete(_url(doc_id, category_id)).status_code == 404


class TestDetailShowsTheCategories:
    def test_with_ids_so_a_client_can_untag(self, env: Env) -> None:
        env.client.put(_url(DOC, BETA))
        env.client.put(_url(DOC, ALPHA))

        body = env.client.get(f"/api/v1/documents/{DOC}").json()

        assert body["tags"] == ["Alpha", "Beta"]  # unchanged, names only
        assert [(c["id"], c["name"]) for c in body["categories"]] == [
            (ALPHA, "Alpha"),
            (BETA, "Beta"),
        ]
        assert body["tag_count"] == 2

    def test_a_document_with_no_tags_has_an_empty_list(self, env: Env) -> None:
        body = env.client.get(f"/api/v1/documents/{DOC}").json()
        assert body["categories"] == []


def test_a_read_key_cannot_tag(tmp_path: Path) -> None:
    with _build(tmp_path, ApiKeyTier.READ) as env:
        assert env.client.put(_url(DOC, ALPHA)).status_code == 403
        assert env.client.delete(_url(OTHER_DOC, BETA)).status_code == 403
        assert env.tags_of(OTHER_DOC) == [(BETA, TaggedBy.LLM, 1.0)]
