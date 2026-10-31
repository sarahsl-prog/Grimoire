"""grimoire.vectorstore.factory: the one place stores are built from settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from grimoire.config.settings import GrimoireSettings, VectorStoreType
from grimoire.vectorstore.factory import (
    UnsupportedVectorStoreError,
    create_vector_store,
    get_maintenance_store,
)


def _settings(tmp_path: Path, **vector_store: Any) -> GrimoireSettings:
    base = GrimoireSettings()
    vs = base.vector_store.model_copy(
        update={
            "chromadb": base.vector_store.chromadb.model_copy(
                update={"path": tmp_path / "chroma", "collection_name": "coll"}
            ),
            **vector_store,
        }
    )
    return base.model_copy(update={"vector_store": vs})


class TestCreateVectorStore:
    def test_maps_settings_onto_the_store(self, tmp_path: Path) -> None:
        store = create_vector_store(_settings(tmp_path, host="chroma", port=8000))
        assert store.persist_directory == tmp_path / "chroma"
        assert store.collection_name == "coll"
        assert store.host == "chroma"
        assert store.port == 8000
        assert not store.is_connected  # unopened: caller picks initialize/connect

    def test_returns_a_new_store_each_time(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        assert create_vector_store(s) is not create_vector_store(s)

    def test_unimplemented_backend_fails_loudly(self, tmp_path: Path) -> None:
        """Qdrant has config models but no store; never fall back silently."""
        with pytest.raises(UnsupportedVectorStoreError, match="qdrant"):
            create_vector_store(_settings(tmp_path, type=VectorStoreType.QDRANT))


class TestMaintenanceStore:
    async def test_is_connected_but_not_write_ready(self, tmp_path: Path) -> None:
        store = await get_maintenance_store(_settings(tmp_path))
        assert store.is_connected
        assert not store.is_initialized  # no fake embedding dimension

    async def test_is_reused_for_the_same_settings(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        assert await get_maintenance_store(s) is await get_maintenance_store(s)

    async def test_differs_when_settings_change(self, tmp_path: Path) -> None:
        a = await get_maintenance_store(_settings(tmp_path))
        other = _settings(tmp_path)
        other = other.model_copy(
            update={
                "vector_store": other.vector_store.model_copy(
                    update={
                        "chromadb": other.vector_store.chromadb.model_copy(
                            update={"collection_name": "other"}
                        )
                    }
                )
            }
        )
        b = await get_maintenance_store(other)
        assert a is not b
        assert b.collection_name == "other"
