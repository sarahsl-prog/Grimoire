"""Recent ingests tab.

Shows the ten most recently created documents.  The API already orders by
created_at descending, so this widget never sorts.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from grimoire.api.schemas import DocumentListResponse, DocumentResponse
from grimoire.gui.errors import GuiError
from grimoire.gui.workers import run_api_call

_COLUMNS = ("Title", "Type", "Status", "Chunks", "Tags", "Size", "Created")
_ROW_LIMIT = 10
_FAILED_ROW_COLOR = QColor(120, 30, 30)
_REFRESH_LABEL = "Refresh"
_REFRESH_BUSY_LABEL = "Refreshing…"


class RecentTab(QWidget):
    """The last ten documents the corpus absorbed.

    Args:
        client: GrimoireClient (or a stub in tests).
        pool: Shared thread pool.
        on_error: Callback that puts a message in the window's status bar.
    """

    def __init__(
        self, client: Any, pool: QThreadPool, on_error: Callable[[str], None]
    ) -> None:
        super().__init__()
        self._client = client
        self._pool = pool
        self._on_error = on_error
        self._in_flight = False

        self.refresh_button = QPushButton(_REFRESH_LABEL)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        top_row = QHBoxLayout()
        top_row.addWidget(self.error_label, stretch=1)
        top_row.addWidget(self.refresh_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self.table, stretch=1)

        self.refresh_button.clicked.connect(self.refresh)

    def set_client(self, client: Any) -> None:
        """Swap in a client rebuilt around a new session key."""
        self._client = client

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        """Refresh when the tab becomes visible.

        No QTimer poll: every key has a per-minute rate limit, and polling a
        window nobody is looking at spends it for nothing.
        """
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        """Re-request the ten most recent documents."""
        if self._in_flight:
            return
        self._in_flight = True
        self.refresh_button.setEnabled(False)
        # Busy state lives on the button, not error_label: that label is
        # reserved for errors, and writing status text into it would make
        # the "did the request finish" checks fire on a placeholder instead
        # of the real result.
        self.refresh_button.setText(_REFRESH_BUSY_LABEL)
        run_api_call(
            self._pool,
            lambda: self._client.recent_documents(limit=_ROW_LIMIT),
            self._on_result,
            self._on_failure,
        )

    def _on_result(self, result: DocumentListResponse) -> None:
        self._in_flight = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText(_REFRESH_LABEL)
        self.table.setRowCount(len(result.documents))
        for row, document in enumerate(result.documents):
            self._fill_row(row, document)
        self.error_label.setText(
            "" if result.documents else "No documents ingested yet."
        )

    def _fill_row(self, row: int, document: DocumentResponse) -> None:
        values = (
            document.title or "(untitled)",
            document.file_type,
            document.processing_status,
            str(document.chunk_count),
            str(document.tag_count),
            _humanize_bytes(document.size_bytes),
            _format_timestamp(document.created_at),
        )
        failed = document.processing_status == "failed"
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(document.source_path)
            if failed:
                item.setForeground(QBrush(_FAILED_ROW_COLOR))
            if column in (3, 4, 5):
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            self.table.setItem(row, column, item)

    def _on_failure(self, error: GuiError) -> None:
        self._in_flight = False
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText(_REFRESH_LABEL)
        self.error_label.setText(error.message)
        self._on_error(error.message)


def _humanize_bytes(size: int) -> str:
    """Render a byte count in the largest unit that keeps it readable."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _format_timestamp(raw: str | None) -> str:
    """Render an API timestamp in local time, falling back to the raw string."""
    if not raw:
        return "-"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
