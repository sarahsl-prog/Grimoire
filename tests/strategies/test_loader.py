"""Tests for the Phase 8 strategy loader.

The loader is the single seam between Grimoire's general and security
domains: callers ask for a chunker/retriever and the loader decides which
one to return based on ``settings.security.domain``.

Coverage:

* ``domain == "security"`` returns the security implementations
  (``SecurityChunker`` / ``SecurityRetriever``).
* ``domain == "general"`` returns ``None`` for both (callers fall back).
* Missing ``security`` block on the settings object also defaults to
  general — guard against legacy YAML predating Phase 7.
* The chunk_config kwarg is forwarded to ``SecurityChunker``.
* ``load_retriever`` wraps the supplied ``HybridSearch`` instance.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from grimoire.core.chunker.base import ChunkConfig
from grimoire.strategies.loader import load_chunker, load_retriever
from grimoire.strategies.security.chunker import SecurityChunker
from grimoire.strategies.security.retriever import SecurityRetriever


def _settings(domain: str | None) -> SimpleNamespace:
    """Build a minimal settings stub with a ``security.domain`` field."""
    if domain is None:
        return SimpleNamespace()  # no `.security` attribute at all
    return SimpleNamespace(security=SimpleNamespace(domain=domain))


class TestLoadChunker:
    def test_security_domain_returns_security_chunker(self) -> None:
        chunker = load_chunker(_settings("security"))
        assert isinstance(chunker, SecurityChunker)

    def test_general_domain_returns_none(self) -> None:
        assert load_chunker(_settings("general")) is None

    def test_missing_security_block_returns_none(self) -> None:
        assert load_chunker(_settings(None)) is None

    def test_empty_domain_string_returns_none(self) -> None:
        """A blank ``domain`` should be treated as general, not security."""
        assert load_chunker(_settings("")) is None

    def test_unknown_domain_returns_none(self) -> None:
        """Any value other than ``"security"`` is treated as general."""
        assert load_chunker(_settings("homelab")) is None

    def test_chunk_config_forwarded_to_security_chunker(self) -> None:
        config = ChunkConfig(chunk_size=1234, chunk_overlap=42)
        chunker = load_chunker(_settings("security"), chunk_config=config)
        assert isinstance(chunker, SecurityChunker)
        # SecurityChunker stores config on the parent class and on the prose
        # fallback; both should reflect the override.
        assert chunker.config.chunk_size == 1234

    def test_no_chunk_config_uses_security_chunker_default(self) -> None:
        chunker = load_chunker(_settings("security"))
        assert isinstance(chunker, SecurityChunker)
        # Default ChunkConfig has a positive chunk_size — sanity-check.
        assert chunker.config.chunk_size > 0


class TestLoadRetriever:
    def test_security_domain_returns_security_retriever(self) -> None:
        hybrid = MagicMock()
        retriever = load_retriever(_settings("security"), hybrid)
        assert isinstance(retriever, SecurityRetriever)
        # The retriever should wrap the supplied hybrid search instance.
        assert retriever._hybrid is hybrid

    def test_general_domain_returns_none(self) -> None:
        assert load_retriever(_settings("general"), MagicMock()) is None

    def test_missing_security_block_returns_none(self) -> None:
        assert load_retriever(_settings(None), MagicMock()) is None

    def test_unknown_domain_returns_none(self) -> None:
        assert load_retriever(_settings("homelab"), MagicMock()) is None


def test_loader_uses_real_grimoire_settings_in_general_mode() -> None:
    """End-to-end smoke test against the real ``GrimoireSettings`` class.

    The default ``GrimoireSettings`` has ``security.domain == "general"`` so
    both loaders should return ``None``.
    """
    from grimoire.config.settings import GrimoireSettings

    settings = GrimoireSettings()
    assert load_chunker(settings) is None
    assert load_retriever(settings, MagicMock()) is None


def test_loader_uses_real_grimoire_settings_in_security_mode() -> None:
    """Switching ``settings.security.domain`` flips the loader output."""
    from grimoire.config.settings import GrimoireSettings, SecurityConfig

    settings = GrimoireSettings()
    settings.security = SecurityConfig(domain="security")

    chunker = load_chunker(settings)
    assert isinstance(chunker, SecurityChunker)

    retriever = load_retriever(settings, MagicMock())
    assert isinstance(retriever, SecurityRetriever)


@pytest.mark.parametrize("domain", ["security", "general", "", "homelab"])
def test_load_chunker_never_raises(domain: str) -> None:
    """The loader is the I/O boundary; it should never raise on bad input."""
    load_chunker(_settings(domain))


@pytest.mark.parametrize("domain", ["security", "general", "", "homelab"])
def test_load_retriever_never_raises(domain: str) -> None:
    load_retriever(_settings(domain), MagicMock())
