"""Drag-and-drop ingest tab.

Files upload one at a time: the pipeline is CPU- and GPU-bound server side,
so parallel uploads would queue on the API anyway while making progress
meaningless.  Progress counts files, not bytes, because parsing and embedding
dominate the wall clock, not the local-network transfer.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from grimoire.api.schemas import IngestResultResponse
from grimoire.gui.config import SUPPORTED_EXTENSIONS, GuiConfig
from grimoire.gui.errors import GuiError
from grimoire.gui.workers import run_api_call

_COLUMNS = ("File", "Status", "Detail")


class DropZone(QFrame):
    """A frame that accepts dropped files and reports their paths."""

    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(110)
        label = QLabel("Drop files here to ingest, or use Browse")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.addWidget(label)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        """Accept the drag only when it carries at least one local file.

        `hasUrls()` alone is also true for a URL dragged out of a browser
        (a non-local, non-file URL): accepting that shows the copy cursor
        and then dropEvent's own `isLocalFile()` filter silently drops
        everything, producing exactly the "a file that vanishes from a
        drop" confusion this tab's docstring says it exists to prevent.
        """
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """Emit the dropped local paths."""
        mime = event.mimeData()
        paths = [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()


class IngestTab(QWidget):
    """Queue dropped files and upload them one by one.

    Args:
        client: GrimoireClient (or a stub in tests).
        config: Supplies the client-side upload cap.
        pool: Shared thread pool.
        on_error: Callback that puts a message in the window's status bar.
    """

    ingest_completed = Signal()

    def __init__(
        self,
        client: Any,
        config: GuiConfig,
        pool: QThreadPool,
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._client = client
        self._config = config
        self._pool = pool
        self._on_error = on_error
        self._pending: deque[tuple[int, Path]] = deque()
        self._uploading = False
        self._batch_total = 0
        self._batch_done = 0
        self._any_success = False
        # The row/path of the upload currently in flight.  Read back by
        # _on_uploaded/_on_failed instead of being closed over in a lambda:
        # PySide6 only keeps a QThreadPool signal's cross-thread delivery
        # alive when the slot is a bound method of a live QObject (self, in
        # this case).  A lambda wrapping that call is just a plain Python
        # callable with no such affinity, and the connection can be dropped
        # before the worker thread's result ever arrives - silently
        # stalling the queue after the first file.  Since uploads are
        # strictly one at a time, "current" state is unambiguous.
        self._current_row: int = -1
        self._current_path: Path | None = None

        self.drop_zone = DropZone()
        self.browse_button = QPushButton("Browse…")
        self.auto_tag_checkbox = QCheckBox("Auto-tag with the LLM")
        self.auto_tag_checkbox.setChecked(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)

        self.queue_table = QTableWidget(0, len(_COLUMNS))
        self.queue_table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        controls = QHBoxLayout()
        controls.addWidget(self.auto_tag_checkbox)
        controls.addStretch(1)
        controls.addWidget(self.browse_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.drop_zone)
        layout.addLayout(controls)
        layout.addWidget(self.progress)
        layout.addWidget(self.error_label)
        layout.addWidget(self.queue_table, stretch=1)

        self.drop_zone.files_dropped.connect(self.enqueue)
        self.browse_button.clicked.connect(self._browse)

    def set_client(self, client: Any) -> None:
        """Swap in a client rebuilt around a new session key."""
        self._client = client

    def shutdown(self) -> None:
        """Stop queuing new uploads as the window closes.

        MainWindow.closeEvent duck-types for this on every tab. IngestTab is
        the only one with a queue: without this, an upload already in
        flight (via ApiWorker's own shutdown guard) still finishes, but
        _finish_one would then pop and start the next _pending entry against
        a window that is going away. Clearing the deque here removes that
        entry point; the in-flight upload's own result is still handled
        safely by ApiWorker (see workers.py).
        """
        self._pending.clear()

    def _browse(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Select files to ingest")
        if names:
            self.enqueue([Path(name) for name in names])

    def enqueue(self, paths: list[Path]) -> None:
        """Add files to the queue, rejecting the ones the server would refuse.

        Rejected files stay visible with their reason: a file that vanishes
        from a drop is more confusing than one that explains itself.
        """
        self.error_label.clear()
        accepted = 0
        for path in paths:
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)
            self._set_row(row, path.name, "queued", "")
            reason = self._rejection_reason(path)
            if reason:
                self._set_row(row, path.name, f"rejected: {reason}", "")
                continue
            self._pending.append((row, path))
            accepted += 1

        if accepted:
            self._batch_total += accepted
            self.progress.setRange(0, self._batch_total)
            self.progress.setValue(self._batch_done)
            self._start_next()

    def _rejection_reason(self, path: Path) -> str | None:
        """Why the server would refuse this file, or None if it would not."""
        if not path.is_file():
            return "Not a file (directories are not uploaded)"
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            return f"Unsupported file type: {suffix or '(no extension)'}"
        if path.stat().st_size > self._config.max_upload_bytes:
            limit_mb = self._config.max_upload_bytes / (1024 * 1024)
            return f"Too large (limit {limit_mb:.0f} MB)"
        return None

    def _start_next(self) -> None:
        """Upload the next queued file, if nothing is already in flight."""
        if self._uploading or not self._pending:
            return
        row, path = self._pending.popleft()
        self._uploading = True
        self._current_row = row
        self._current_path = path
        self._set_row(row, path.name, "uploading", "")
        auto_tag = self.auto_tag_checkbox.isChecked()
        run_api_call(
            self._pool,
            lambda: self._client.upload(path, auto_tag=auto_tag),
            self._on_uploaded,
            self._on_failed,
        )

    def _on_uploaded(self, result: IngestResultResponse) -> None:
        row, path = self._current_row, self._current_path
        if path is None:  # pragma: no cover - set by _start_next before this fires
            raise RuntimeError("_on_uploaded fired with no upload in flight")
        if result.status == "failed":
            self._set_row(
                row, path.name, "failed", result.error_message or "Ingestion failed"
            )
        elif result.status == "skipped":
            self._set_row(row, path.name, "skipped", "Already in the corpus")
            self._any_success = True
        else:
            self._set_row(
                row,
                path.name,
                "done",
                f"{result.chunks_created} chunks · {result.duration_ms} ms",
            )
            self._any_success = True
        self._finish_one()

    def _on_failed(self, error: GuiError) -> None:
        row, path = self._current_row, self._current_path
        if path is None:  # pragma: no cover - set by _start_next before this fires
            raise RuntimeError("_on_failed fired with no upload in flight")
        self._set_row(row, path.name, "failed", error.message)
        self.error_label.setText(error.message)
        self._on_error(error.message)
        self._finish_one()

    def _finish_one(self) -> None:
        """Advance the batch: one failure never aborts the rest."""
        self._uploading = False
        self._batch_done += 1
        self.progress.setValue(self._batch_done)
        if self._pending:
            self._start_next()
            return
        if self._any_success:
            self.ingest_completed.emit()
        self._batch_total = 0
        self._batch_done = 0
        self._any_success = False
        self.progress.setRange(0, 1)
        self.progress.setValue(1)

    def _set_row(self, row: int, name: str, status: str, detail: str) -> None:
        for column, value in enumerate((name, status, detail)):
            self.queue_table.setItem(row, column, QTableWidgetItem(value))
