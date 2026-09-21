"""Search / Ask tab.

Two modes over one query box.  Ask runs the full RAG pipeline; Search runs
retrieval only, which is the fast way to check whether the right chunks come
back before spending an LLM generation on them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from grimoire.api.schemas import QueryResponse, SearchResponse
from grimoire.gui.errors import GuiError
from grimoire.gui.widgets.citation_card import CitationCard
from grimoire.gui.workers import run_api_call


class SearchTab(QWidget):
    """Query the corpus and inspect what retrieval returned.

    Args:
        client: GrimoireClient (or a stub in tests).
        pool: Shared thread pool; no request runs on the GUI thread.
        on_error: Callback that puts a message in the window's status bar.
    """

    def __init__(
        self, client: Any, pool: QThreadPool, on_error: Callable[[str], None]
    ) -> None:
        super().__init__()
        self._client = client
        self._pool = pool
        self._on_error = on_error
        # True once the user edits top_k directly. Until then, the spinner
        # tracks each mode's own client default (ask=5, search=10) rather
        # than freezing at whichever default happened to show first.
        self._top_k_touched = False

        self.query_field = QLineEdit()
        self.query_field.setPlaceholderText("Ask a question, or search for a phrase")
        self.submit_button = QPushButton("Run")
        self.ask_radio = QRadioButton("Ask")
        self.search_radio = QRadioButton("Search")
        self.ask_radio.setChecked(True)
        self.ask_radio.setToolTip("Retrieval plus a generated answer")
        self.search_radio.setToolTip("Retrieval only - no LLM, much faster")
        self.top_k_spin = QSpinBox()
        self.top_k_spin.setRange(1, 100)
        self.top_k_spin.setValue(5)
        self.top_k_spin.setPrefix("top_k ")
        self.cache_checkbox = QCheckBox("Use cache")
        self.cache_checkbox.setChecked(True)

        controls = QHBoxLayout()
        controls.addWidget(self.ask_radio)
        controls.addWidget(self.search_radio)
        controls.addWidget(self.top_k_spin)
        controls.addWidget(self.cache_checkbox)
        controls.addStretch(1)

        query_row = QHBoxLayout()
        query_row.addWidget(self.query_field, stretch=1)
        query_row.addWidget(self.submit_button)

        self.answer_view = QTextBrowser()
        self.answer_view.setOpenExternalLinks(False)
        self.answer_view.setMinimumHeight(180)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.footer_label = QLabel()

        results_container = QWidget()
        self.results_layout = QVBoxLayout(results_container)
        self.results_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(results_container)

        layout = QVBoxLayout(self)
        layout.addLayout(query_row)
        layout.addLayout(controls)
        layout.addWidget(self.error_label)
        layout.addWidget(self.answer_view)
        layout.addWidget(self.footer_label)
        layout.addWidget(scroll, stretch=1)

        self.submit_button.clicked.connect(self.submit)
        self.query_field.returnPressed.connect(self.submit)
        self.ask_radio.toggled.connect(self._sync_mode)
        self.top_k_spin.valueChanged.connect(self._on_top_k_edited)
        self._sync_mode()

    def set_client(self, client: Any) -> None:
        """Swap in a client rebuilt around a new session key."""
        self._client = client

    def _sync_mode(self) -> None:
        """Hide the answer panel in Search mode: there is no answer."""
        is_ask = self.ask_radio.isChecked()
        self.answer_view.setVisible(is_ask)
        self.cache_checkbox.setEnabled(is_ask)
        if not self._top_k_touched:
            self.top_k_spin.blockSignals(True)
            self.top_k_spin.setValue(5 if is_ask else 10)
            self.top_k_spin.blockSignals(False)

    def _on_top_k_edited(self) -> None:
        """Stop following the mode default once the user picks their own."""
        self._top_k_touched = True

    def submit(self) -> None:
        """Run the current mode's query, unless the box is empty."""
        query = self.query_field.text().strip()
        if not query:
            return

        self._clear_results()
        self.error_label.clear()
        self.footer_label.setText("Working…")
        self.submit_button.setEnabled(False)

        if self.ask_radio.isChecked():
            top_k = self.top_k_spin.value()
            use_cache = self.cache_checkbox.isChecked()
            run_api_call(
                self._pool,
                lambda: self._client.ask(query, top_k=top_k, use_cache=use_cache),
                self._on_ask_result,
                self._on_failure,
            )
        else:
            top_k = self.top_k_spin.value()
            run_api_call(
                self._pool,
                lambda: self._client.search(query, top_k=top_k),
                self._on_search_result,
                self._on_failure,
            )

    def _on_ask_result(self, result: QueryResponse) -> None:
        self.submit_button.setEnabled(True)
        self.answer_view.setMarkdown(result.answer or "_(no answer returned)_")
        for citation in result.citations:
            self.results_layout.addWidget(
                CitationCard(
                    citation.document_title or "(untitled)",
                    citation.relevance_score,
                    citation.content_snippet,
                    citation.document_id,
                )
            )
        cached = "cached" if result.cached else "fresh"
        self.footer_label.setText(
            f"{len(result.citations)} sources · {result.model_used} · "
            f"{cached} · {result.duration_ms} ms"
        )
        if not result.citations:
            self._show_empty("No sources matched this question.")

    def _on_search_result(self, result: SearchResponse) -> None:
        self.submit_button.setEnabled(True)
        for item in result.results:
            self.results_layout.addWidget(
                CitationCard(
                    item.document_title or "(untitled)",
                    item.score,
                    item.content,
                    item.document_id,
                )
            )
        self.footer_label.setText(
            f"{result.total_results} results · {result.duration_ms} ms"
        )
        if not result.results:
            self._show_empty("No chunks matched that search.")

    def _on_failure(self, error: GuiError) -> None:
        self.submit_button.setEnabled(True)
        self.footer_label.clear()
        self.error_label.setText(error.message)
        self._on_error(error.message)

    def _show_empty(self, message: str) -> None:
        self.results_layout.addWidget(QLabel(message))

    def _clear_results(self) -> None:
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
