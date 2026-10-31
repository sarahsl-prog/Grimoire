"""Integration test: document delete cascades to ChromaDB vectors.

Verifies that DELETE /api/v1/documents/{id} removes all associated vectors
from ChromaDB so the document is no longer searchable (C-1).
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from grimoire.api.main import create_app
from grimoire.db.models import ApiKey, ApiKeyTier, Chunk, Document
from grimoire.vectorstore import ChromaDBStore


def _make_test_api_key() -> ApiKey:
    return ApiKey(
        id="test-key-12345678",
        name="test-key",
        tier=ApiKeyTier.AGENT,
        key_prefix="grim_agt_tst",
        key_hash="$2b$12$fakehash",
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def temp_chroma_path() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        yield Path(tmp)


@pytest_asyncio.fixture
async def chroma_store(temp_chroma_path: Path) -> ChromaDBStore:
    store = ChromaDBStore(
        persist_directory=temp_chroma_path,
        collection_name="test_collection",
        distance_metric="cosine",
    )
    await store.initialize(collection_name="test_collection", embedding_dim=384)
    return store


@pytest.fixture
def app_with_real_chroma(chroma_store: ChromaDBStore, temp_chroma_path: Path):
    """FastAPI app whose delete handler talks to a real temporary ChromaDB."""
    test_app = create_app(use_lifespan=False)
    if hasattr(test_app.state, "limiter") and test_app.state.limiter:
        test_app.state.limiter.enabled = False

    from grimoire.api.auth import get_api_key
    from grimoire.config.settings import GrimoireSettings

    test_key = _make_test_api_key()

    async def override_api_key():
        return test_key

    # Build a settings object that points at our temp ChromaDB.
    settings = GrimoireSettings()
    settings.vector_store.chromadb.path = str(temp_chroma_path)
    settings.vector_store.chromadb.collection_name = "test_collection"

    # Patch the global settings singleton so get_settings() returns our temp config.
    import sys

    _settings_mod = sys.modules["grimoire.config.settings"]

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(_settings_mod, "_settings", settings)
        test_app.dependency_overrides[get_api_key] = override_api_key
        yield test_app
        test_app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_document_removes_vectors(
    app_with_real_chroma,
    chroma_store: ChromaDBStore,
):
    """Ingest vectors, delete the document via API, assert ChromaDB is empty."""
    # Must be a UUID: the route 404s anything else (get_by_uuid).
    doc_id = "7d3c1f0e-8a2b-4c5d-9e6f-0123456789ab"
    vector_ids = ["vec-1", "vec-2", "vec-3"]
    embeddings = [[0.1] * 384, [0.2] * 384, [0.3] * 384]
    metadatas = [
        {"doc_id": doc_id, "chunk_idx": 0},
        {"doc_id": doc_id, "chunk_idx": 1},
        {"doc_id": doc_id, "chunk_idx": 2},
    ]
    documents = ["chunk one", "chunk two", "chunk three"]

    # Seed ChromaDB with vectors for this document.
    await chroma_store.add_documents(
        ids=vector_ids,
        embeddings=embeddings,
        metadatas=metadatas,
        documents=documents,
    )

    # Verify seeded.
    results = await chroma_store.get(vector_ids)
    assert len(results) == 3

    # Build a mock Document with chunks that carry the same vector_ids.
    mock_doc = MagicMock(spec=Document)
    mock_doc.id = doc_id
    mock_doc.chunks = []
    for vid in vector_ids:
        chunk = MagicMock(spec=Chunk)
        chunk.vector_id = vid
        mock_doc.chunks.append(chunk)

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=mock_doc)
    mock_session.delete = AsyncMock(return_value=None)
    mock_session.commit = AsyncMock(return_value=None)
    mock_session.rollback = AsyncMock(return_value=None)

    async def override_db():
        yield mock_session

    from grimoire.api.dependencies import get_db_session

    app_with_real_chroma.dependency_overrides[get_db_session] = override_db

    with TestClient(app_with_real_chroma, raise_server_exceptions=False) as c:
        resp = c.delete(f"/api/v1/documents/{doc_id}")

    app_with_real_chroma.dependency_overrides.clear()

    assert resp.status_code == 204

    # Assert vectors are gone from ChromaDB.
    results_after = await chroma_store.get(vector_ids)
    assert len(results_after) == 0, "Vectors should be deleted from ChromaDB"
