"""Build vector stores from settings.

This is the one place that turns ``settings.vector_store`` into a store, so
the API, MCP server, watcher and CLI can't drift apart (audit finding C-5:
the same construction block was copy-pasted into seven call sites).

Two entry points:

* :func:`create_vector_store` returns a new, unopened store. Agents that
  write or search use it and call ``initialize()`` with their embedder's
  dimension.
* :func:`get_maintenance_store` returns a shared, opened store for
  delete/count/get/health checks. It is opened with ``connect()``, so it
  never needs a made-up embedding dimension, and it is reused instead of
  creating a new client on every request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from grimoire.config.settings import VectorStoreType, get_settings
from grimoire.vectorstore import chromadb as _chromadb
from grimoire.vectorstore.chromadb import ChromaDBStore

if TYPE_CHECKING:
    from grimoire.config.settings import GrimoireSettings

# Opened maintenance stores, keyed by the settings that define them, so a
# settings change (or a test that patches settings) gets a fresh store
# instead of a stale one.
_StoreKey = tuple[str, str, str | None, int | None]
_maintenance_stores: dict[_StoreKey, ChromaDBStore] = {}


class UnsupportedVectorStoreError(ValueError):
    """Raised when settings select a backend Grimoire doesn't implement."""


def _key(settings: GrimoireSettings) -> _StoreKey:
    vs = settings.vector_store
    return (str(vs.chromadb.path), vs.chromadb.collection_name, vs.host, vs.port)


def create_vector_store(settings: GrimoireSettings | None = None) -> ChromaDBStore:
    """Build a new, unopened vector store from settings.

    Args:
        settings: Settings to use; defaults to :func:`get_settings`.

    Returns:
        A ChromaDBStore. Call ``initialize()`` (to write or search) or
        ``connect()`` (maintenance only) before use.

    Raises:
        UnsupportedVectorStoreError: If ``vector_store.type`` names a backend
            with no implementation. Qdrant has config models but no store;
            failing here beats silently using ChromaDB instead.
    """
    settings = settings or get_settings()
    vs = settings.vector_store
    if vs.type != VectorStoreType.CHROMADB:
        raise UnsupportedVectorStoreError(
            f"vector_store.type={vs.type.value!r} is not implemented; "
            "only 'chromadb' is supported"
        )
    # Looked up through the module at call time so tests can patch
    # grimoire.vectorstore.chromadb.ChromaDBStore in one place.
    return _chromadb.ChromaDBStore(
        persist_directory=vs.chromadb.path,
        collection_name=vs.chromadb.collection_name,
        host=vs.host,
        port=vs.port,
    )


async def get_maintenance_store(
    settings: GrimoireSettings | None = None,
) -> ChromaDBStore:
    """Return a shared store opened for delete/count/get/health checks.

    The store is opened with ``connect()`` and cached per configuration.
    Concurrent first calls may both open a store; the last one is kept,
    which is harmless (both point at the same collection).

    Args:
        settings: Settings to use; defaults to :func:`get_settings`.

    Raises:
        UnsupportedVectorStoreError: See :func:`create_vector_store`.
        ConnectionError: If the vector store can't be reached.
    """
    settings = settings or get_settings()
    key = _key(settings)
    store = _maintenance_stores.get(key)
    if store is None or not store.is_connected:
        store = create_vector_store(settings)
        await store.connect()
        _maintenance_stores[key] = store
        logger.debug(f"Opened maintenance vector store for {key[1]!r}")
    return store


def clear_maintenance_stores() -> None:
    """Forget cached maintenance stores (tests, or after a config reload)."""
    _maintenance_stores.clear()
