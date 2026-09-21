"""Tests for the Search/Ask tab."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QThreadPool  # noqa: E402

from grimoire.api.schemas import (  # noqa: E402
    CitationResponse,
    QueryResponse,
    SearchResponse,
    SearchResultItem,
)
from grimoire.gui.errors import ConnectionFailed  # noqa: E402
from grimoire.gui.widgets.search_tab import SearchTab  # noqa: E402


class _StubClient:
    def __init__(self) -> None:
        self.ask_calls: list[tuple[str, int, bool]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.ask_error: Exception | None = None

    def ask(self, query, *, top_k=5, use_cache=True):
        self.ask_calls.append((query, top_k, use_cache))
        if self.ask_error:
            raise self.ask_error
        return QueryResponse(
            query=query,
            answer="Because of X.",
            citations=[
                CitationResponse(
                    document_id="doc-1",
                    document_title="Primer",
                    chunk_id="c1",
                    chunk_index=0,
                    content_snippet="X is...",
                    relevance_score=0.77,
                )
            ],
            model_used="llama3",
            search_results_count=1,
            cached=True,
            duration_ms=1500,
        )

    def search(self, query, *, top_k=10):
        self.search_calls.append((query, top_k))
        return SearchResponse(
            query=query,
            results=[
                SearchResultItem(
                    chunk_id="c1",
                    document_id="doc-1",
                    document_title="Primer",
                    content="X is...",
                    score=0.66,
                )
            ],
            total_results=1,
            duration_ms=40,
        )


def _make_tab(qtbot, client=None, errors=None):
    tab = SearchTab(
        client or _StubClient(),
        QThreadPool(),
        errors.append if errors is not None else (lambda _m: None),
    )
    qtbot.addWidget(tab)
    return tab


def _card_count(tab) -> int:
    return tab.results_layout.count()


class TestAskMode:
    def test_renders_answer_and_cards(self, qtbot) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.query_field.setText("why x")
        tab.ask_radio.setChecked(True)

        tab.submit()
        qtbot.waitUntil(lambda: _card_count(tab) > 0, timeout=3000)

        assert "Because of X." in tab.answer_view.toPlainText()
        assert client.ask_calls == [("why x", 5, True)]
        assert "llama3" in tab.footer_label.text()
        assert "cached" in tab.footer_label.text().lower()

    def test_passes_top_k_and_cache_flag(self, qtbot) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.query_field.setText("why x")
        tab.top_k_spin.setValue(12)
        tab.cache_checkbox.setChecked(False)

        tab.submit()
        qtbot.waitUntil(lambda: bool(client.ask_calls), timeout=3000)

        assert client.ask_calls == [("why x", 12, False)]


class TestSearchMode:
    def test_renders_cards_without_answer_panel(self, qtbot) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.query_field.setText("x")
        tab.search_radio.setChecked(True)

        tab.submit()
        qtbot.waitUntil(lambda: _card_count(tab) > 0, timeout=3000)

        # The spinner is the single source of truth for top_k in both modes
        # (it defaults to 5 and is never rewritten by a mode switch), so a
        # Search call with the spinner untouched carries 5, not the
        # client's own default of 10.
        assert client.search_calls == [("x", 5)]
        assert not tab.answer_view.isVisible()

    def test_top_k_value_is_unchanged_by_mode_toggling(self, qtbot) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.query_field.setText("x")
        tab.top_k_spin.setValue(7)

        tab.ask_radio.setChecked(True)
        tab.search_radio.setChecked(True)
        tab.ask_radio.setChecked(True)

        assert tab.top_k_spin.value() == 7

        tab.search_radio.setChecked(True)
        tab.submit()
        qtbot.waitUntil(lambda: bool(client.search_calls), timeout=3000)

        assert client.search_calls == [("x", 7)]


class TestEmptyAndErrorStates:
    def test_blank_query_makes_no_request(self, qtbot) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.query_field.setText("   ")

        tab.submit()

        assert client.ask_calls == []
        assert client.search_calls == []

    def test_error_shows_inline_and_reenables_button(self, qtbot) -> None:
        client = _StubClient()
        client.ask_error = ConnectionFailed("Cannot reach the Grimoire API")
        errors: list[str] = []
        tab = _make_tab(qtbot, client, errors)
        tab.query_field.setText("why x")

        tab.submit()
        qtbot.waitUntil(lambda: bool(tab.error_label.text()), timeout=3000)

        assert "Cannot reach" in tab.error_label.text()
        assert tab.submit_button.isEnabled()
        assert errors and "Cannot reach" in errors[0]
