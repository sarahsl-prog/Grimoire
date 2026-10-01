"""Tests for the Documents pane.

As with the Search pane, the client is a stub whose every call can be scripted
or held in flight.  The risky properties are stale responses, paging at the
edges, and document-controlled text (titles come from ingested files).
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from textual.widgets import DataTable, Footer, Select, TabbedContent  # noqa: E402

from grimoire.api.schemas import DocumentListResponse, DocumentResponse  # noqa: E402
from grimoire.gui.errors import (  # noqa: E402
    AuthFailed,
    ConnectionFailed,
    ServerError,
)
from grimoire.tui.app import GrimoireApp  # noqa: E402
from grimoire.tui.errors import GENERIC_ERROR  # noqa: E402
from grimoire.tui.screens.document_detail import DocumentDetailScreen  # noqa: E402
from grimoire.tui.widgets.documents_pane import PAGE_SIZE, DocumentsPane  # noqa: E402
from grimoire.tui.widgets.status_bar import StatusBar  # noqa: E402

from .conftest import screen_text  # noqa: E402


def _doc(i: int, **overrides: Any) -> DocumentResponse:
    fields: dict[str, Any] = {
        "id": f"doc-{i}",
        "title": f"Document {i}",
        "source_path": f"/srv/data/file{i}.pdf",
        "file_type": "pdf",
        "storage_backend": "local",
        "processing_status": "completed",
        "size_bytes": 2048,
        "created_at": "2026-10-01T10:30:00",
        "updated_at": "2026-10-01T10:35:00",
    }
    fields.update(overrides)
    return DocumentResponse(**fields)


def _page(n: int = 3, *, total: int | None = None, offset: int = 0, start: int = 0):
    return DocumentListResponse(
        documents=[_doc(start + i) for i in range(n)],
        total=n if total is None else total,
        offset=offset,
        limit=PAGE_SIZE,
    )


def _pane(app: GrimoireApp) -> DocumentsPane:
    return app.query_one(DocumentsPane)


def _table(app: GrimoireApp) -> DataTable[Any]:
    return app.query_one("#documents-table", DataTable)


async def _settled(app: GrimoireApp, pilot: Any) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def _until(pilot: Any, predicate: Any, timeout: float = 5.0) -> None:
    """Wait for a condition, not for a fixed number of event-loop turns.

    Showing the tab is a chain of deferred steps (tab activation, a deferred
    pane hook, a worker, a posted message), so "two pauses" is a guess about how
    long that takes.  Polling a real condition is both faster and not flaky.
    """
    import time

    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await pilot.pause(0.01)


async def _launched(app: GrimoireApp, pilot: Any) -> None:
    """Let startup finish before the test touches the keyboard.

    Startup queues a focus on the Search box, and Textual delivers the matching
    focus event a few turns later; a tab switch pressed before it lands is
    overridden by it (focusing a widget activates its tab).  A person cannot
    press a key that fast, so this is a test-harness race, not a product one.
    """
    await _until(pilot, lambda: app.focused is not None)
    for _ in range(6):
        await pilot.pause()


def _list_calls(app: GrimoireApp) -> int:
    return len(getattr(app._client, "calls_to", lambda _m: [])("list_documents"))


async def _open(app: GrimoireApp, pilot: Any, ready: Any = None) -> None:
    """Show the Documents tab, which triggers the first load, and let it finish.

    ``ready`` overrides what "the load has started" means, for a client that
    does not record calls (the real one).
    """
    await _launched(app, pilot)
    wanted = _list_calls(app) + 1
    await pilot.press("f2")
    await _until(pilot, ready or (lambda: _list_calls(app) >= wanted))
    await _settled(app, pilot)
    # Keys sent before the pane has the keyboard go to whatever had it before.
    pane = _pane(app)
    await _until(
        pilot,
        lambda: app.focused is not None and pane in app.focused.ancestors_with_self,
    )


def _cells(app: GrimoireApp, row: int) -> list[str]:
    table = _table(app)
    key = list(table.rows)[row]
    return [
        cell.plain if hasattr(cell, "plain") else str(cell)
        for cell in table.get_row(key)
    ]


class TestFirstLoad:
    async def test_nothing_is_fetched_until_the_tab_is_shown(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            assert stub_client.calls_to("list_documents") == []

    async def test_populates_rows_with_formatted_columns(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(3))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert _table(app).row_count == 3
            assert _cells(app, 0) == [
                "Document 0",
                "pdf",
                "completed",
                "2.0 KB",
                "2026-10-01 10:30",
            ]

    async def test_the_first_request_is_page_zero_with_no_filters(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

        ((_, kwargs),) = stub_client.calls_to("list_documents")
        assert kwargs == {
            "offset": 0,
            "limit": PAGE_SIZE,
            "status": None,
            "file_type": None,
        }

    async def test_showing_the_tab_again_does_not_reload(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("f1")
            await pilot.pause()
            await pilot.press("f2")
            for _ in range(6):  # a reload, if it were coming, would be here by now
                await pilot.pause()
            await _settled(app, pilot)

            assert app.query_one(TabbedContent).active == "documents"
            assert len(stub_client.calls_to("list_documents")) == 1

    async def test_the_footer_shows_the_range_and_page(self, stub_client, step) -> None:
        stub_client.documents_script = [step(_page(50, total=312))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert _pane(app).footer_text == "Rows 1-50 of 312 \u00b7 page 1/7"

    async def test_the_table_has_the_keyboard_after_loading(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert app.focused is _table(app)


class TestEmptyAndCounts:
    async def test_an_empty_result_shows_a_message_instead_of_a_blank_grid(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(0, total=0))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert not _table(app).display
            assert app.query_one("#documents-empty").display
            assert "No documents match" in screen_text(app)
            assert _pane(app).footer_text == "No documents."

    async def test_rows_returning_hides_the_empty_message(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(0, total=0)), step(_page(2))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("ctrl+r")
            await _settled(app, pilot)

            assert _table(app).display
            assert not app.query_one("#documents-empty").display

    async def test_an_empty_result_leaves_the_keyboard_on_the_refresh_button(
        self, stub_client, step
    ) -> None:
        """Hiding the focused table makes Textual drop focus; without a hand-off
        the keyboard would end up on nothing and no key would work."""
        stub_client.documents_script = [step(_page(0, total=0))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert app.focused is app.query_one("#doc-refresh")

    async def test_the_table_gets_the_keyboard_back_when_rows_return(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(0, total=0)), step(_page(2))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("ctrl+r")
            await _settled(app, pilot)

            assert app.focused is _table(app)

    async def test_focus_is_not_taken_when_the_user_is_elsewhere(
        self, stub_client, step
    ) -> None:
        """A reload landing while the user is typing in a filter must not steal
        the keyboard from it."""
        stub_client.documents_script = [step(_page(3)), step(_page(0, total=0))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            app.query_one("#status-filter", Select).focus()
            await pilot.pause()

            await pilot.press("ctrl+r")
            await _settled(app, pilot)

            assert app.focused is app.query_one("#status-filter")

    @pytest.mark.parametrize(
        ("total", "pages"), [(1, 1), (50, 1), (51, 2), (100, 2), (101, 3), (312, 7)]
    )
    async def test_the_page_count_rounds_up(
        self, stub_client, step, total: int, pages: int
    ) -> None:
        stub_client.documents_script = [step(_page(min(total, 50), total=total))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert _pane(app).footer_text.endswith(f"page 1/{pages}")


class TestRowsAndKeys:
    async def test_row_keys_are_document_ids(self, stub_client, step) -> None:
        stub_client.documents_script = [step(_page(3))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert [k.value for k in _table(app).rows] == ["doc-0", "doc-1", "doc-2"]

    async def test_selected_document_id_follows_the_cursor(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(3))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            assert _pane(app).selected_document_id == "doc-0"

            await pilot.press("down")
            assert _pane(app).selected_document_id == "doc-1"

    async def test_selected_document_id_is_none_when_there_are_no_rows(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(0, total=0))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert _pane(app).selected_document_id is None

    async def test_duplicate_ids_from_the_server_do_not_crash_the_table(
        self, stub_client, step
    ) -> None:
        resp = DocumentListResponse(
            documents=[_doc(1), _doc(1, title="again"), _doc(2)], total=3
        )
        stub_client.documents_script = [step(resp)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert app.is_running
            assert _table(app).row_count == 3


class TestTitles:
    @pytest.mark.parametrize(
        ("title", "path", "expected"),
        [
            (None, "/srv/data/report.pdf", "report.pdf"),
            ("", "/srv/data/report.pdf", "report.pdf"),
            ("   ", "/srv/data/report.pdf", "report.pdf"),
            (None, "C:\\Users\\sam\\docs\\plan.docx", "plan.docx"),
            (None, "gdrive://folder/sub/notes.md", "notes.md"),
            (None, "bare.txt", "bare.txt"),
            (None, "", "(untitled)"),
            (None, "/trailing/slash/", "(untitled)"),
            ("Real title", "/srv/data/report.pdf", "Real title"),
        ],
    )
    async def test_title_falls_back_to_the_file_name_only(
        self, stub_client, step, title, path, expected
    ) -> None:
        resp = DocumentListResponse(
            documents=[_doc(1, title=title, source_path=path)], total=1
        )
        stub_client.documents_script = [step(resp)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert _cells(app, 0)[0] == expected

    async def test_the_full_server_path_is_never_shown(self, stub_client, step) -> None:
        resp = DocumentListResponse(
            documents=[_doc(1, title=None, source_path="/srv/secret/dir/f.pdf")],
            total=1,
        )
        stub_client.documents_script = [step(resp)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(140, 40)) as pilot:
            await _open(app, pilot)

            assert "secret" not in screen_text(app)

    async def test_a_long_title_is_truncated_to_one_line(
        self, stub_client, step
    ) -> None:
        resp = DocumentListResponse(documents=[_doc(1, title="word " * 100)], total=1)
        stub_client.documents_script = [step(resp)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            title = _cells(app, 0)[0]
            assert len(title) <= 60 and title.endswith("\u2026")

    async def test_hostile_text_renders_literally_in_every_cell(
        self, stub_client, step
    ) -> None:
        hostile = "[bold red]boom[/] [link=http://evil.example]x[/link] [/nope]"
        resp = DocumentListResponse(
            documents=[
                _doc(
                    1,
                    title=hostile,
                    file_type=hostile,
                    processing_status=hostile,
                )
            ],
            total=1,
        )
        stub_client.documents_script = [step(resp)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(200, 40)) as pilot:
            await _open(app, pilot)

            assert app.is_running
            cells = _cells(app, 0)
            assert cells[0].startswith("[bold red]boom[/]")
            assert cells[1].startswith("[bold red]boom[/] [link")
            assert cells[2].startswith("[bold red]boom[/] [link")
            assert "[bold red]boom[/]" in screen_text(app)


class TestStatusColours:
    @pytest.mark.parametrize(
        ("status", "style"),
        [
            ("completed", "green"),
            ("failed", "red"),
            ("pending", "dim"),
            ("processing", "dim"),
            ("stale", "dim"),
            ("something-new", "dim"),
        ],
    )
    async def test_status_cell_style(
        self, stub_client, step, status: str, style: str
    ) -> None:
        resp = DocumentListResponse(
            documents=[_doc(1, processing_status=status)], total=1
        )
        stub_client.documents_script = [step(resp)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            cell = _table(app).get_row(list(_table(app).rows)[0])[2]
            assert str(cell.style) == style
            assert cell.plain == status


class TestPaging:
    async def test_offsets_advance_by_the_page_size(self, stub_client, step) -> None:
        stub_client.documents_script = [
            step(_page(50, total=120, start=0)),
            step(_page(50, total=120, start=50)),
            step(_page(20, total=120, start=100)),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("right_square_bracket")
            await _settled(app, pilot)
            await pilot.press("right_square_bracket")
            await _settled(app, pilot)

            offsets = [k["offset"] for _, k in stub_client.calls_to("list_documents")]
            assert offsets == [0, 50, 100]
            assert _pane(app).footer_text == "Rows 101-120 of 120 \u00b7 page 3/3"

    async def test_next_on_the_last_page_is_a_no_op(self, stub_client, step) -> None:
        stub_client.documents_script = [step(_page(50, total=50))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            await pilot.press("right_square_bracket")
            await _settled(app, pilot)

            assert len(stub_client.calls_to("list_documents")) == 1

    async def test_previous_on_the_first_page_is_a_no_op(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(50, total=120))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            await pilot.press("left_square_bracket")
            await _settled(app, pilot)

            assert len(stub_client.calls_to("list_documents")) == 1

    async def test_the_page_actions_clamp_themselves_when_called_directly(
        self, stub_client, step
    ) -> None:
        """The hidden key binding already blocks these, so the actions' own
        clamps are a second line of defence; this exercises them directly."""
        stub_client.documents_script = [step(_page(50, total=50))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            pane = _pane(app)

            pane.action_next_page()  # already on the only page
            pane.action_prev_page()  # already on the first page
            await _settled(app, pilot)

            assert len(stub_client.calls_to("list_documents")) == 1

    async def test_previous_goes_back_one_page(self, stub_client, step) -> None:
        stub_client.documents_script = [
            step(_page(50, total=120)),
            step(_page(50, total=120, start=50)),
            step(_page(50, total=120)),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("right_square_bracket")
            await _settled(app, pilot)
            await pilot.press("left_square_bracket")
            await _settled(app, pilot)

            offsets = [k["offset"] for _, k in stub_client.calls_to("list_documents")]
            assert offsets == [0, 50, 0]

    async def test_paging_bindings_are_hidden_when_they_cannot_apply(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(50, total=120))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            pane = _pane(app)

            assert pane.check_action("next_page", ()) is True
            assert pane.check_action("prev_page", ()) is False

    async def test_a_page_past_the_end_falls_back_to_the_last_real_page(
        self, stub_client, step
    ) -> None:
        """Documents were deleted elsewhere between two page loads."""
        stub_client.documents_script = [
            step(_page(50, total=120, start=0)),
            step(_page(50, total=120, start=50)),
            # page 3 was requested but the corpus has since shrunk to 60 rows:
            step(DocumentListResponse(documents=[], total=60, offset=100)),
            step(_page(10, total=60, start=50)),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("right_square_bracket")
            await _settled(app, pilot)
            await pilot.press("right_square_bracket")
            await _settled(app, pilot)

            offsets = [k["offset"] for _, k in stub_client.calls_to("list_documents")]
            assert offsets == [0, 50, 100, 50]
            assert _table(app).row_count == 10
            assert _pane(app).footer_text == "Rows 51-60 of 60 \u00b7 page 2/2"

    async def test_an_empty_page_with_a_zero_total_does_not_loop(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [
            step(_page(50, total=100)),
            step(DocumentListResponse(documents=[], total=0, offset=50)),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("right_square_bracket")
            await _settled(app, pilot)

            assert len(stub_client.calls_to("list_documents")) == 2
            assert "No documents match" in screen_text(app)


class TestFilters:
    async def test_a_status_filter_is_sent_and_resets_to_page_zero(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [
            step(_page(50, total=120)),
            step(_page(50, total=120, start=50)),
            step(_page(2, total=2)),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("right_square_bracket")
            await _settled(app, pilot)

            app.query_one("#status-filter", Select).value = "failed"
            await _settled(app, pilot)

        last = stub_client.calls_to("list_documents")[-1][1]
        assert last == {
            "offset": 0,
            "limit": PAGE_SIZE,
            "status": "failed",
            "file_type": None,
        }

    async def test_both_filters_combine(self, stub_client, step) -> None:
        stub_client.documents_script = [step(_page()), step(_page()), step(_page())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            app.query_one("#status-filter", Select).value = "completed"
            await _settled(app, pilot)
            app.query_one("#type-filter", Select).value = "md"
            await _settled(app, pilot)

        last = stub_client.calls_to("list_documents")[-1][1]
        assert (last["status"], last["file_type"]) == ("completed", "md")

    async def test_clearing_a_filter_sends_none_again(self, stub_client, step) -> None:
        stub_client.documents_script = [step(_page()), step(_page()), step(_page())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            app.query_one("#status-filter", Select).value = "failed"
            await _settled(app, pilot)
            app.query_one("#status-filter", Select).clear()
            await _settled(app, pilot)

        assert stub_client.calls_to("list_documents")[-1][1]["status"] is None

    async def test_mounting_the_selects_does_not_trigger_a_request(
        self, stub_client, step
    ) -> None:
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            assert stub_client.calls == []


class TestRefresh:
    async def test_ctrl_r_reloads_the_current_page(self, stub_client, step) -> None:
        stub_client.documents_script = [
            step(_page(50, total=120)),
            step(_page(50, total=120, start=50)),
            step(_page(50, total=120, start=50)),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("right_square_bracket")
            await _settled(app, pilot)

            await pilot.press("ctrl+r")
            await _settled(app, pilot)

            offsets = [k["offset"] for _, k in stub_client.calls_to("list_documents")]
            assert offsets == [0, 50, 50]

    async def test_the_refresh_button_reloads_too(self, stub_client, step) -> None:
        stub_client.documents_script = [step(_page()), step(_page())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            await pilot.click("#doc-refresh")
            await _settled(app, pilot)

            assert len(stub_client.calls_to("list_documents")) == 2

    async def test_the_cursor_stays_on_the_same_document(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(5)), step(_page(5))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("down", "down")
            assert _pane(app).selected_document_id == "doc-2"

            await pilot.press("ctrl+r")
            await _settled(app, pilot)

            assert _pane(app).selected_document_id == "doc-2"

    async def test_the_cursor_follows_the_document_when_rows_move(
        self, stub_client, step
    ) -> None:
        """A new document arrived at the top: doc-2 is now the fourth row."""
        reloaded = DocumentListResponse(
            documents=[_doc(99), _doc(0), _doc(1), _doc(2), _doc(3)], total=5
        )
        stub_client.documents_script = [step(_page(5)), step(reloaded)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("down", "down")

            await pilot.press("ctrl+r")
            await _settled(app, pilot)

            assert _pane(app).selected_document_id == "doc-2"

    async def test_the_cursor_is_clamped_when_its_document_disappears(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(5)), step(_page(2))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("down", "down", "down", "down")
            assert _pane(app).selected_document_id == "doc-4"

            await pilot.press("ctrl+r")
            await _settled(app, pilot)

            assert _pane(app).selected_document_id == "doc-1"  # last remaining row


class TestErrors:
    @pytest.mark.parametrize(
        "error",
        [
            ConnectionFailed("Cannot reach the Grimoire API at http://x - is it up?"),
            AuthFailed("API key rejected. Check GRIMOIRE_API_KEY."),
            ServerError("Grimoire API error - check the API logs."),
        ],
    )
    async def test_an_error_shows_its_message_and_keeps_the_table(
        self, stub_client, step, error
    ) -> None:
        stub_client.documents_script = [step(_page(3)), step(error)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("ctrl+r")
            await _settled(app, pilot)

            assert app.is_running
            assert _pane(app).status_text == error.message
            assert _table(app).row_count == 3
            assert _pane(app).footer_text == "Rows 1-3 of 3 \u00b7 page 1/1"

    async def test_an_unexpected_exception_shows_only_the_generic_message(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(RuntimeError("secret /srv/app/x.py"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert _pane(app).status_text == GENERIC_ERROR

    async def test_a_failed_page_change_leaves_the_pane_on_the_page_it_shows(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [
            step(_page(50, total=120)),
            step(ServerError("boom")),
            step(_page(50, total=120, start=50)),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("right_square_bracket")  # fails
            await _settled(app, pilot)
            assert _pane(app).footer_text == "Rows 1-50 of 120 \u00b7 page 1/3"

            await pilot.press("right_square_bracket")  # retry: still page 2
            await _settled(app, pilot)

            offsets = [k["offset"] for _, k in stub_client.calls_to("list_documents")]
            assert offsets == [0, 50, 50]

    async def test_the_next_success_clears_the_error(self, stub_client, step) -> None:
        stub_client.documents_script = [step(ServerError("boom")), step(_page())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            assert _pane(app).status_text == "boom"

            await pilot.press("ctrl+r")
            await _settled(app, pilot)

            assert _pane(app).status_text == ""

    async def test_error_notifications_disable_markup(self, stub_client, step) -> None:
        from grimoire.gui.errors import RequestRejected

        stub_client.documents_script = [step(RequestRejected("bad [/nonexistent] x"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        seen: list[dict[str, Any]] = []
        original = app.notify

        def spy(message: str, **kwargs: Any) -> None:
            seen.append({"message": message, **kwargs})
            original(message, **kwargs)

        app.notify = spy  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert app.is_running
        errors = [n for n in seen if n.get("severity") == "error"]
        assert errors and all(n.get("markup") is False for n in errors)

    async def test_a_failed_first_load_can_be_retried_by_showing_the_tab_again(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(ServerError("boom")), step(_page(2))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("f1")
            await pilot.pause()
            await pilot.press("f2")
            await _until(pilot, lambda: _list_calls(app) >= 2)
            await _settled(app, pilot)

            assert _table(app).row_count == 2


class TestStaleResponses:
    async def test_a_slow_early_response_cannot_overwrite_a_newer_one(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.documents_script = [
            step(_page(3, start=0), gate=gate),  # A: slow
            step(_page(2, start=100)),  # B: fast, issued second
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _launched(app, pilot)
            await pilot.press("f2")
            await _until(pilot, lambda: _list_calls(app) >= 1)
            await pilot.press("ctrl+r")  # supersedes A with B
            await pilot.pause()
            await pilot.pause()
            assert [k.value for k in _table(app).rows] == ["doc-100", "doc-101"]

            gate.set()  # A finally returns
            await _settled(app, pilot)

            assert [k.value for k in _table(app).rows] == ["doc-100", "doc-101"]

    async def test_a_slow_early_error_cannot_replace_a_newer_success(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.documents_script = [
            step(ServerError("OLD FAILURE"), gate=gate),
            step(_page(2)),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _launched(app, pilot)
            await pilot.press("f2")
            await _until(pilot, lambda: _list_calls(app) >= 1)
            await pilot.press("ctrl+r")
            await _until(pilot, lambda: _list_calls(app) >= 2)
            await pilot.pause()

            gate.set()
            await _settled(app, pilot)

            assert _pane(app).status_text == ""
            assert _table(app).row_count == 2

    async def test_rapid_filter_changes_keep_only_the_last(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.documents_script = [
            step(_page(1, start=0)),  # initial load
            step(_page(5, start=10), gate=gate),  # status=failed, slow
            step(_page(2, start=20)),  # file_type=md, fast
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            app.query_one("#status-filter", Select).value = "failed"
            await pilot.pause()
            app.query_one("#type-filter", Select).value = "md"
            await pilot.pause()
            await pilot.pause()

            gate.set()
            await _settled(app, pilot)

            assert [k.value for k in _table(app).rows] == ["doc-20", "doc-21"]


class TestConnectionReporting:
    async def test_a_connection_failure_marks_the_api_unreachable(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(ConnectionFailed("Cannot reach the API."))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            assert app.query_one(StatusBar).state == "connected"

            await _open(app, pilot)

            assert app.query_one(StatusBar).state == "unreachable"

    async def test_a_success_marks_the_api_connected(self, stub_client, step) -> None:
        stub_client.healthy = False
        stub_client.documents_script = [step(_page())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            assert app.query_one(StatusBar).state == "unreachable"

            await _open(app, pilot)

            assert app.query_one(StatusBar).state == "connected"

    async def test_an_http_error_means_the_server_is_up(
        self, stub_client, step
    ) -> None:
        stub_client.healthy = False
        stub_client.documents_script = [step(AuthFailed("nope"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await _open(app, pilot)

            assert app.query_one(StatusBar).state == "connected"


class TestWithTheRealClient:
    """Through httpx, pydantic and a real socket: no stubbed client."""

    async def test_paging_and_filters_reach_the_server_as_query_parameters(
        self,
    ) -> None:
        import json
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import parse_qs, urlsplit

        from grimoire.gui.client import GrimoireClient
        from grimoire.gui.config import GuiConfig

        seen: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                parts = urlsplit(self.path)
                if parts.path == "/health":
                    self.send_response(200)
                    self.end_headers()
                    return
                params = {k: v[0] for k, v in parse_qs(parts.query).items()}
                seen.append(
                    {
                        "path": parts.path,
                        "params": params,
                        "api_key": self.headers.get("X-API-Key"),
                        "session": self.headers.get("X-Session-Id"),
                    }
                )
                offset = int(params.get("offset", 0))
                body = _page(
                    min(50, 120 - offset), total=120, offset=offset, start=offset
                ).model_dump()
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config = GuiConfig(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            api_key="grim_agt_real",
            session_id="sess-docs",
        )
        client = GrimoireClient(config)
        try:
            app = GrimoireApp(client, config, None)
            async with app.run_test() as pilot:
                await _open(app, pilot, ready=lambda: _table(app).row_count > 0)
                assert _table(app).row_count == 50
                await pilot.press("right_square_bracket")
                await _settled(app, pilot)
                app.query_one("#status-filter", Select).value = "failed"
                await _settled(app, pilot)
        finally:
            client.close()
            server.shutdown()

        assert [s["params"] for s in seen] == [
            {"offset": "0", "limit": "50"},
            {"offset": "50", "limit": "50"},
            {"offset": "0", "limit": "50", "status": "failed"},
        ]
        assert all(s["path"] == "/api/v1/documents" for s in seen)
        assert all(s["api_key"] == "grim_agt_real" for s in seen)
        assert all(s["session"] == "sess-docs" for s in seen)


class TestLayout:
    async def test_the_range_line_is_not_hidden_under_the_key_bar(
        self, stub_client, step
    ) -> None:
        """Regression: TabbedContent was one row taller than the space left for
        it, so the last row of every pane sat underneath the docked footer."""
        stub_client.documents_script = [step(_page(3, total=312))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(app, pilot)

            footer_line = app.query_one("#doc-footer")
            key_bar = app.query_one(Footer)
            assert footer_line.region.bottom <= key_bar.region.y
            assert "Rows 1-3 of 312" in screen_text(app)

    async def test_the_table_fills_the_space_down_to_the_range_line(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(3, total=3))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(100, 30)) as pilot:
            await _open(app, pilot)

            table = app.query_one("#documents-table")
            assert table.region.bottom == app.query_one("#doc-footer").region.y


class TestOpenDetail:
    @staticmethod
    def _client_with_detail(stub_client: Any, step: Any, pages: list[Any]) -> None:
        from grimoire.api.schemas import DocumentDetailResponse

        stub_client.documents_script = pages
        stub_client.detail_script = [
            step(
                DocumentDetailResponse(
                    id="x",
                    source_path="/p/x.pdf",
                    file_type="pdf",
                    storage_backend="local",
                    processing_status="completed",
                )
            )
            for _ in range(4)
        ]

    async def test_enter_opens_the_detail_for_the_row_under_the_cursor(
        self, stub_client, step
    ) -> None:
        self._client_with_detail(stub_client, step, [step(_page(3))])
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            await pilot.press("enter")
            await _until(pilot, lambda: isinstance(app.screen, DocumentDetailScreen))

            assert stub_client.calls_to("get_document")[0][0] == ("doc-0",)

    async def test_enter_on_a_later_row_opens_that_document(
        self, stub_client, step
    ) -> None:
        self._client_with_detail(stub_client, step, [step(_page(3))])
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            await pilot.press("down", "down", "enter")
            await _until(pilot, lambda: isinstance(app.screen, DocumentDetailScreen))

            assert stub_client.calls_to("get_document")[0][0] == ("doc-2",)

    async def test_a_row_without_an_id_cannot_be_opened(
        self, stub_client, step
    ) -> None:
        resp = DocumentListResponse(documents=[_doc(1, id="")], total=1)
        self._client_with_detail(stub_client, step, [step(resp)])
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            await pilot.press("enter")
            for _ in range(4):
                await pilot.pause()

            assert not isinstance(app.screen, DocumentDetailScreen)
            assert stub_client.calls_to("get_document") == []

    async def test_closing_returns_the_keyboard_and_cursor_to_the_table(
        self, stub_client, step
    ) -> None:
        self._client_with_detail(stub_client, step, [step(_page(4))])
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("down", "enter")
            await _until(pilot, lambda: isinstance(app.screen, DocumentDetailScreen))
            await _settled(app, pilot)

            await pilot.press("escape")
            await _until(
                pilot, lambda: not isinstance(app.screen, DocumentDetailScreen)
            )
            await pilot.pause()

            assert app.focused is _table(app)
            assert _pane(app).selected_document_id == "doc-1"
