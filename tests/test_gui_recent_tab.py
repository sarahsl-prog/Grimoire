"""Tests for the Recent ingests tab."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QThreadPool  # noqa: E402

from grimoire.api.schemas import DocumentListResponse, DocumentResponse  # noqa: E402
from grimoire.gui.errors import AuthFailed  # noqa: E402
from grimoire.gui.widgets.recent_tab import RecentTab  # noqa: E402


def _doc(doc_id: str, status: str = "completed") -> DocumentResponse:
    return DocumentResponse(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_path=f"/app/uploads/{doc_id}.md",
        file_type="md",
        storage_backend="local",
        processing_status=status,
        size_bytes=4096,
        created_at="2026-09-21T10:00:00Z",
        updated_at="2026-09-21T10:00:01Z",
        tag_count=2,
        chunk_count=6,
    )


class _StubClient:
    def __init__(self, docs=None, error=None) -> None:
        self.calls = 0
        self.docs = docs if docs is not None else [_doc("a"), _doc("b")]
        self.error = error

    def recent_documents(self, *, limit=10):
        self.calls += 1
        if self.error:
            raise self.error
        return DocumentListResponse(
            documents=self.docs, total=len(self.docs), offset=0, limit=limit
        )


class TestRecentTab:
    def test_refresh_populates_rows(self, qtbot) -> None:
        client = _StubClient()
        tab = RecentTab(client, QThreadPool(), lambda _m: None)
        qtbot.addWidget(tab)

        tab.refresh()
        qtbot.waitUntil(lambda: tab.table.rowCount() == 2, timeout=3000)

        assert tab.table.item(0, 0).text() == "Doc a"
        assert tab.table.item(0, 3).text() == "6"  # chunk count column

    def test_failed_document_is_marked(self, qtbot) -> None:
        client = _StubClient(docs=[_doc("bad", status="failed")])
        tab = RecentTab(client, QThreadPool(), lambda _m: None)
        qtbot.addWidget(tab)

        tab.refresh()
        qtbot.waitUntil(lambda: tab.table.rowCount() == 1, timeout=3000)

        assert tab.table.item(0, 2).text() == "failed"

    def test_empty_corpus_shows_no_rows(self, qtbot) -> None:
        client = _StubClient(docs=[])
        tab = RecentTab(client, QThreadPool(), lambda _m: None)
        qtbot.addWidget(tab)

        tab.refresh()
        qtbot.waitUntil(lambda: bool(tab.error_label.text()), timeout=3000)

        assert tab.table.rowCount() == 0
        assert "No documents" in tab.error_label.text()

    def test_error_is_reported_inline_and_to_the_window(self, qtbot) -> None:
        errors: list[str] = []
        client = _StubClient(error=AuthFailed("API key rejected."))
        tab = RecentTab(client, QThreadPool(), errors.append)
        qtbot.addWidget(tab)

        tab.refresh()
        qtbot.waitUntil(lambda: bool(tab.error_label.text()), timeout=3000)

        assert "rejected" in tab.error_label.text()
        assert errors == ["API key rejected."]

    def test_showing_the_tab_refreshes_once(self, qtbot) -> None:
        client = _StubClient()
        tab = RecentTab(client, QThreadPool(), lambda _m: None)
        qtbot.addWidget(tab)

        tab.show()
        qtbot.waitUntil(lambda: client.calls >= 1, timeout=3000)

        assert client.calls == 1, "tab activation must not stack requests"

    def test_refresh_button_shows_busy_and_resets(self, qtbot) -> None:
        # Success path.
        client = _StubClient()
        tab = RecentTab(client, QThreadPool(), lambda _m: None)
        qtbot.addWidget(tab)

        assert tab.refresh_button.text() == "Refresh"
        assert tab.refresh_button.isEnabled()

        tab.refresh()
        # The worker's result is delivered via a queued signal, which only
        # runs once the event loop is pumped - so immediately after refresh()
        # returns, the button must still show the busy state it set
        # synchronously.
        assert tab.refresh_button.text() == "Refreshing…"
        assert not tab.refresh_button.isEnabled()

        qtbot.waitUntil(lambda: tab.table.rowCount() == 2, timeout=3000)
        assert tab.refresh_button.text() == "Refresh"
        assert tab.refresh_button.isEnabled()

        # Same reset on the failure path.
        client = _StubClient(error=AuthFailed("API key rejected."))
        tab = RecentTab(client, QThreadPool(), lambda _m: None)
        qtbot.addWidget(tab)

        tab.refresh()
        assert tab.refresh_button.text() == "Refreshing…"
        assert not tab.refresh_button.isEnabled()

        qtbot.waitUntil(lambda: bool(tab.error_label.text()), timeout=3000)
        assert tab.refresh_button.text() == "Refresh"
        assert tab.refresh_button.isEnabled()
