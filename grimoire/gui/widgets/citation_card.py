"""One retrieved chunk, rendered as a card.

Shared by both query modes: an Ask shows the chunks its answer rests on, a
Search shows the same chunks with no answer above them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

_SNIPPET_LIMIT = 600


class CitationCard(QFrame):
    """A source chunk with its score and a copyable document id.

    Args:
        title: Document title, or a placeholder when the document has none.
        score: Relevance score, shown to two decimals.
        snippet: Chunk text; truncated for display.
        document_id: Copied to the clipboard by the card's button, so a
            result can be followed up with the CLI or the API.
    """

    def __init__(
        self, title: str, score: float, snippet: str, document_id: str
    ) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.document_id = document_id

        header = QHBoxLayout()
        title_label = QLabel(f"<b>{title or '(untitled)'}</b>")
        title_label.setWordWrap(True)
        score_label = QLabel(f"{score:.2f}")
        copy_button = QPushButton("Copy ID")
        copy_button.setToolTip(document_id)
        copy_button.clicked.connect(self._copy_id)
        header.addWidget(title_label, stretch=1)
        header.addWidget(score_label)
        header.addWidget(copy_button)

        body = QLabel(self._truncate(snippet))
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            body.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
        )

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(body)

    @staticmethod
    def _truncate(snippet: str) -> str:
        text = snippet.strip()
        if len(text) <= _SNIPPET_LIMIT:
            return text
        return f"{text[:_SNIPPET_LIMIT]}…"

    def _copy_id(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.document_id)
