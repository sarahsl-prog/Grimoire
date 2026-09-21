"""Tests for the CitationCard widget."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import Qt  # noqa: E402

from grimoire.gui.widgets.citation_card import CitationCard  # noqa: E402


class TestMarkupIsShownVerbatim:
    """The corpus ingests .html/.htm, so markup in a title or snippet is
    expected input, not an attack - but this tab exists precisely to show
    what retrieval actually returned, so it must never interpret that
    markup as rich text.
    """

    def test_snippet_markup_is_displayed_verbatim(self, qtbot) -> None:
        snippet = "<h1>Heading</h1><p>body <b>bold</b></p>"
        card = CitationCard("Title", 0.5, snippet, "doc-1")
        qtbot.addWidget(card)

        # Walk the layout directly rather than relying on object names.
        body = card.layout().itemAt(1).widget()

        assert body.text() == snippet
        assert body.textFormat() == Qt.TextFormat.PlainText

    def test_title_markup_is_displayed_verbatim(self, qtbot) -> None:
        title = "<img src=x>Title"
        card = CitationCard(title, 0.5, "snippet", "doc-1")
        qtbot.addWidget(card)

        header_layout = card.layout().itemAt(0).layout()
        title_label = header_layout.itemAt(0).widget()

        assert title_label.text() == title
        assert title_label.textFormat() == Qt.TextFormat.PlainText

    def test_untitled_placeholder_still_used_for_empty_title(self, qtbot) -> None:
        card = CitationCard("", 0.5, "snippet", "doc-1")
        qtbot.addWidget(card)

        header_layout = card.layout().itemAt(0).layout()
        title_label = header_layout.itemAt(0).widget()

        assert title_label.text() == "(untitled)"
