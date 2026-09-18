"""Tests for grimoire/cli/helpers.py agent-builder wiring.

Guards against the vector store's remote/local mode silently falling back
to a per-container embedded ChromaDB even when a shared ``chromadb``
service is configured. ``build_ingestion_agent`` and ``build_query_agent``
are the only two places in the codebase that construct ``ChromaDBStore``
(the CLI's ingest/query/watch commands and the API's dependency-injection
layer all route through them), so this is the single place a regression
here would need to be caught.
"""

from __future__ import annotations

import importlib

import pytest

from grimoire.cli.helpers import build_ingestion_agent, build_query_agent
from grimoire.vectorstore.chromadb import ChromaDBStore


@pytest.fixture
def clean_settings_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the cached settings singleton for the duration of a test.

    Mirrors the fixture of the same name in test_cli.py: the re-exported
    ``settings`` proxy shadows the submodule attribute on the package, so
    the module must be resolved via import_module, and monkeypatch restores
    the previous instance afterwards so a test's env vars cannot leak.
    """
    settings_module = importlib.import_module("grimoire.config.settings")
    monkeypatch.setattr(settings_module, "_settings", None)


class TestIngestionAgentVectorStoreWiring:
    def test_uses_http_client_when_host_configured(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings_cache: None
    ) -> None:
        """A configured host must select ChromaDB's HTTP client, not a local path.

        This is the container topology: every service shares the `chromadb`
        service over HTTP rather than writing to its own ephemeral
        filesystem.
        """
        monkeypatch.setenv("GRIMOIRE_VECTOR_STORE__HOST", "chromadb")
        monkeypatch.setenv("GRIMOIRE_VECTOR_STORE__PORT", "8000")

        agent = build_ingestion_agent()
        store = agent._vector_store

        assert isinstance(store, ChromaDBStore)
        assert store.host == "chromadb"
        assert store.port == 8000

    def test_uses_embedded_store_without_host(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings_cache: None
    ) -> None:
        """Bare-metal behavior must be unchanged when no host is configured."""
        monkeypatch.delenv("GRIMOIRE_VECTOR_STORE__HOST", raising=False)
        monkeypatch.delenv("GRIMOIRE_VECTOR_STORE__PORT", raising=False)

        agent = build_ingestion_agent()
        store = agent._vector_store

        assert isinstance(store, ChromaDBStore)
        assert store.host is None
        assert store.port is None


class TestQueryAgentVectorStoreWiring:
    def test_uses_http_client_when_host_configured(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings_cache: None
    ) -> None:
        monkeypatch.setenv("GRIMOIRE_VECTOR_STORE__HOST", "chromadb")
        monkeypatch.setenv("GRIMOIRE_VECTOR_STORE__PORT", "8000")

        agent = build_query_agent()
        store = agent._hybrid_search._vector_store

        assert isinstance(store, ChromaDBStore)
        assert store.host == "chromadb"
        assert store.port == 8000

    def test_uses_embedded_store_without_host(
        self, monkeypatch: pytest.MonkeyPatch, clean_settings_cache: None
    ) -> None:
        monkeypatch.delenv("GRIMOIRE_VECTOR_STORE__HOST", raising=False)
        monkeypatch.delenv("GRIMOIRE_VECTOR_STORE__PORT", raising=False)

        agent = build_query_agent()
        store = agent._hybrid_search._vector_store

        assert isinstance(store, ChromaDBStore)
        assert store.host is None
        assert store.port is None
