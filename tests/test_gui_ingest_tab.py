"""Tests for the drag-and-drop ingest tab."""

from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QMimeData, QPoint, Qt, QThreadPool, QUrl  # noqa: E402
from PySide6.QtGui import QDragEnterEvent  # noqa: E402

from grimoire.api.schemas import IngestResultResponse  # noqa: E402
from grimoire.gui.config import GuiConfig  # noqa: E402
from grimoire.gui.errors import RequestRejected  # noqa: E402
from grimoire.gui.widgets.ingest_tab import DropZone, IngestTab  # noqa: E402


class _StubClient:
    def __init__(self, error_for: str | None = None) -> None:
        self.uploaded: list[tuple[Path, bool]] = []
        self.error_for = error_for

    def upload(self, path: Path, *, auto_tag: bool = True) -> IngestResultResponse:
        self.uploaded.append((path, auto_tag))
        if self.error_for and path.name == self.error_for:
            raise RequestRejected(f"Unsupported file type: {path.suffix}")
        return IngestResultResponse(
            file_path=str(path),
            document_id=f"doc-{path.stem}",
            status="completed",
            chunks_created=3,
            vectors_stored=3,
            tags_applied=1,
            error_message=None,
            duration_ms=800,
        )


def _make_tab(qtbot, client, max_bytes: int = 100 * 1024 * 1024):
    config = GuiConfig(
        base_url="http://testapi:8001", api_key="k", max_upload_bytes=max_bytes
    )
    tab = IngestTab(client, config, QThreadPool(), lambda _m: None)
    qtbot.addWidget(tab)
    return tab


def _statuses(tab) -> list[str]:
    return [
        tab.queue_table.item(row, 1).text() for row in range(tab.queue_table.rowCount())
    ]


class TestQueueFiltering:
    def test_unsupported_extension_is_rejected_without_upload(
        self, qtbot, tmp_path
    ) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        bad = tmp_path / "payload.exe"
        bad.write_bytes(b"MZ")

        tab.enqueue([bad])
        qtbot.waitUntil(lambda: bool(_statuses(tab)), timeout=3000)

        assert client.uploaded == []
        assert "unsupported" in _statuses(tab)[0].lower()

    def test_oversized_file_is_rejected_without_upload(self, qtbot, tmp_path) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client, max_bytes=4)
        big = tmp_path / "big.md"
        big.write_bytes(b"12345678")

        tab.enqueue([big])
        qtbot.waitUntil(lambda: bool(_statuses(tab)), timeout=3000)

        assert client.uploaded == []
        assert "too large" in _statuses(tab)[0].lower()

    def test_directory_is_rejected(self, qtbot, tmp_path) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)

        tab.enqueue([tmp_path])
        qtbot.waitUntil(lambda: bool(_statuses(tab)), timeout=3000)

        assert client.uploaded == []


def _drag_enter_event(urls: list[QUrl]) -> QDragEnterEvent:
    mime = QMimeData()
    mime.setUrls(urls)
    event = QDragEnterEvent(
        QPoint(0, 0),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    # QDragEnterEvent does not keep `mime` alive from Python's side, so
    # without a reference held here it can be garbage-collected while the
    # event (which only wraps a C++ pointer to it) is still in use -
    # reproduced as a segfault, not a Python exception.
    event._mime = mime  # type: ignore[attr-defined]
    return event


class TestDropZoneDragFiltering:
    """A drag that carries no local file must not show the copy cursor.

    Before this fix, `dragEnterEvent` accepted any `hasUrls()` drag - true
    even for a URL dragged out of a browser tab - so the cursor promised a
    drop that `dropEvent`'s own `isLocalFile()` filter would then silently
    do nothing with.
    """

    def test_non_local_url_is_not_accepted(self, qtbot) -> None:
        zone = DropZone()
        qtbot.addWidget(zone)
        event = _drag_enter_event([QUrl("https://example.com/report.pdf")])

        zone.dragEnterEvent(event)

        assert not event.isAccepted()

    def test_local_file_url_is_accepted(self, qtbot, tmp_path) -> None:
        zone = DropZone()
        qtbot.addWidget(zone)
        sample = tmp_path / "a.md"
        sample.write_text("a")
        event = _drag_enter_event([QUrl.fromLocalFile(str(sample))])

        zone.dragEnterEvent(event)

        assert event.isAccepted()


class TestShutdown:
    def test_shutdown_empties_a_nonempty_pending_queue(self, qtbot, tmp_path) -> None:
        """Closing mid-upload must not let the queue start another upload.

        MainWindow.closeEvent duck-types for `shutdown()` on every tab;
        IngestTab is the only tab with a queue and, before this fix, had no
        such method. Uploading is one-at-a-time, so right after enqueueing
        two files the first is already in flight and the second still sits
        in `_pending` - shutdown() must clear it.
        """
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        first = tmp_path / "a.md"
        second = tmp_path / "b.md"
        first.write_text("a")
        second.write_text("b")

        tab.enqueue([first, second])
        assert tab._pending, "the second file should still be queued"

        tab.shutdown()

        assert tab._pending == deque()

    def test_shutdown_is_a_noop_on_an_empty_queue(self, qtbot) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)

        tab.shutdown()

        assert tab._pending == deque()


class TestUploading:
    def test_uploads_supported_files_sequentially(self, qtbot, tmp_path) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        first = tmp_path / "a.md"
        second = tmp_path / "b.md"
        first.write_text("a")
        second.write_text("b")

        tab.enqueue([first, second])
        qtbot.waitUntil(lambda: len(client.uploaded) == 2, timeout=5000)
        qtbot.waitUntil(
            lambda: all("done" in s.lower() for s in _statuses(tab)), timeout=5000
        )

        assert [p.name for p, _ in client.uploaded] == ["a.md", "b.md"]
        assert tab.progress.value() == tab.progress.maximum()

    def test_passes_auto_tag_setting(self, qtbot, tmp_path) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.auto_tag_checkbox.setChecked(False)
        sample = tmp_path / "a.md"
        sample.write_text("a")

        tab.enqueue([sample])
        qtbot.waitUntil(lambda: bool(client.uploaded), timeout=3000)

        assert client.uploaded[0][1] is False

    def test_emits_completion_signal_for_recent_tab(self, qtbot, tmp_path) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        sample = tmp_path / "a.md"
        sample.write_text("a")

        with qtbot.waitSignal(tab.ingest_completed, timeout=5000):
            tab.enqueue([sample])

    def test_failure_is_recorded_per_file_and_batch_continues(
        self, qtbot, tmp_path
    ) -> None:
        client = _StubClient(error_for="b.md")
        tab = _make_tab(qtbot, client)
        for name in ("a.md", "b.md", "c.md"):
            (tmp_path / name).write_text("x")

        tab.enqueue([tmp_path / "a.md", tmp_path / "b.md", tmp_path / "c.md"])
        qtbot.waitUntil(lambda: len(client.uploaded) == 3, timeout=6000)
        qtbot.waitUntil(
            lambda: all(
                any(word in s.lower() for word in ("done", "failed"))
                for s in _statuses(tab)
            ),
            timeout=6000,
        )

        statuses = _statuses(tab)
        assert "done" in statuses[0].lower()
        assert "failed" in statuses[1].lower()
        assert "done" in statuses[2].lower(), "one failure must not abort the batch"
