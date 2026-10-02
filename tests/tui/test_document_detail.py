"""Tests for the document detail modal.

The modal fetches one document and shows it as a key/value grid.  Everything it
shows comes from the database or from ingested files, so the properties that
matter are: nothing is parsed as markup, a failure is shown without closing the
modal, and closing while a request is in flight neither raises nor leaks.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from textual.widgets import Button, Static  # noqa: E402

from grimoire.api.schemas import DocumentDetailResponse  # noqa: E402
from grimoire.client.errors import (  # noqa: E402
    AuthFailed,
    ConnectionFailed,
    MalformedResponse,
    RateLimited,
    RequestRejected,
    ServerError,
    TimedOut,
)
from grimoire.tui.app import GrimoireApp  # noqa: E402
from grimoire.tui.errors import GENERIC_ERROR  # noqa: E402
from grimoire.tui.screens.document_detail import DocumentDetailScreen  # noqa: E402
from grimoire.tui.widgets.status_bar import StatusBar  # noqa: E402

from .conftest import StubClient, screen_text  # noqa: E402


def _detail(**overrides: Any) -> DocumentDetailResponse:
    fields: dict[str, Any] = {
        "id": "doc-1",
        "title": "Sigma primer",
        "source_path": "/srv/data/sigma/primer.pdf",
        "file_type": "pdf",
        "storage_backend": "local",
        "processing_status": "completed",
        "size_bytes": 1536,
        "created_at": "2026-10-01T10:30:00",
        "updated_at": "2026-10-02T08:15:00",
        "error_message": None,
        "tags": [],
        "chunk_count": 0,
    }
    fields.update(overrides)
    return DocumentDetailResponse(**fields)


async def _until(pilot: Any, predicate: Any, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the condition")
        await pilot.pause(0.01)


async def _launched(app: GrimoireApp, pilot: Any) -> None:
    await _until(pilot, lambda: app.focused is not None)
    for _ in range(4):
        await pilot.pause()


async def _show(
    app: GrimoireApp, pilot: Any, client: StubClient, doc_id: str = "doc-1"
):
    """Open the modal directly and wait for its fetch to have been issued."""
    await _launched(app, pilot)
    screen = DocumentDetailScreen(client, doc_id)  # type: ignore[arg-type]
    await app.push_screen(screen)
    await _until(pilot, lambda: len(client.calls_to("get_document")) >= 1)
    return screen


async def _settled(app: GrimoireApp, pilot: Any) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


def _base_bar(app: GrimoireApp) -> StatusBar:
    """The status bar lives on the base screen, beneath the modal."""
    return app.screen_stack[0].query_one(StatusBar)


class TestLoading:
    async def test_shows_a_loading_state_until_the_response_arrives(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.detail_script = [step(_detail(), gate=gate)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)

            assert "Loading" in screen.status_text
            assert screen.values["title"] == "-"

            gate.set()
            await _settled(app, pilot)
            assert screen.status_text == ""
            assert screen.values["title"] == "Sigma primer"

    async def test_fetches_exactly_the_requested_document_once(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [step(_detail(id="doc-42"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client, "doc-42")
            await _settled(app, pilot)

        assert stub_client.calls_to("get_document") == [(("doc-42",), {})]


class TestSuccess:
    async def test_shows_every_field_formatted(self, stub_client, step) -> None:
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert screen.values == {
                "id": "doc-1",
                "title": "Sigma primer",
                "source_path": "/srv/data/sigma/primer.pdf",
                "file_type": "pdf",
                "storage_backend": "local",
                "processing_status": "completed",
                "size": "1.5 KB",
                "created": "2026-10-01 10:30",
                "updated": "2026-10-02 08:15",
                "chunks": "0",
                "tags": "-",
                "error": "-",
            }

    async def test_the_full_source_path_is_shown_here_unlike_the_table(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [step(_detail(source_path="/srv/very/deep/f.pdf"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert "/srv/very/deep/f.pdf" in screen_text(app)

    @pytest.mark.parametrize("title", [None, "", "   "])
    async def test_a_missing_title_reads_untitled(
        self, stub_client, step, title
    ) -> None:
        stub_client.detail_script = [step(_detail(title=title))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert screen.values["title"] == "(untitled)"

    async def test_missing_optional_fields_render_as_a_dash(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [
            step(_detail(created_at=None, updated_at=None, storage_backend=""))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert screen.values["created"] == "-"
            assert screen.values["updated"] == "-"
            assert screen.values["storage_backend"] == "-"


class TestTagsAndChunks:
    async def test_tags_are_listed_and_chunks_counted(self, stub_client, step) -> None:
        stub_client.detail_script = [
            step(_detail(tags=["Alpha", "Beta"], chunk_count=42))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert screen.values["tags"] == "Alpha, Beta"
            assert screen.values["chunks"] == "42"
            assert str(app.screen.query_one("#detail-tags", Static).content) == (
                "Alpha, Beta"
            )

    async def test_blank_tag_names_are_dropped(self, stub_client, step) -> None:
        stub_client.detail_script = [step(_detail(tags=["Alpha", "", "  ", "Beta"]))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert screen.values["tags"] == "Alpha, Beta"

    async def test_only_blank_tags_read_as_a_dash(self, stub_client, step) -> None:
        stub_client.detail_script = [step(_detail(tags=["", " "]))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert screen.values["tags"] == "-"

    async def test_a_hostile_tag_is_shown_literally(self, stub_client, step) -> None:
        stub_client.detail_script = [
            step(_detail(tags=["[bold red]x[/] [link=http://evil]c[/link] [/nope]"]))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert app.is_running
            assert "[/nope]" in screen_text(app)

    async def test_a_huge_tag_list_is_capped(self, stub_client, step) -> None:
        stub_client.detail_script = [
            step(_detail(tags=[f"tag-{i}" for i in range(5000)]))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert len(screen.values["tags"]) <= 5_000


class TestWidgetsMatchState:
    """The ``values`` dict is the screen's own bookkeeping.  These check what is
    actually in each widget, which is what a person sees."""

    async def test_every_cell_widget_shows_its_value(self, stub_client, step) -> None:
        stub_client.detail_script = [step(_detail(error_message="parse failed"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            for key, expected in screen.values.items():
                cell = app.screen.query_one(f"#detail-{key}", Static)
                assert str(cell.content) == expected, key

    async def test_the_title_value_does_not_overwrite_the_heading(
        self, stub_client, step
    ) -> None:
        """Regression: the heading and the Title cell once shared an element id,
        so the document's title replaced the word "Document"."""
        stub_client.detail_script = [step(_detail(title="Sigma primer"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert (
                str(app.screen.query_one("#detail-heading", Static).content)
                == "Document"
            )
            assert (
                str(app.screen.query_one("#detail-title", Static).content)
                == "Sigma primer"
            )

    async def test_element_ids_are_unique(self, stub_client, step) -> None:
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            ids = [w.id for w in app.screen.query("*") if w.id]
            assert len(ids) == len(set(ids))


class TestErrorMessageRow:
    async def test_hidden_when_the_document_has_no_error(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [step(_detail(error_message=None))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert not app.screen.query_one("#detail-error").display
            assert not app.screen.query_one("#detail-error-key").display

    async def test_shown_when_set(self, stub_client, step) -> None:
        stub_client.detail_script = [
            step(_detail(error_message="parse failed: bad xref"))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert app.screen.query_one("#detail-error").display
            assert screen.values["error"] == "parse failed: bad xref"
            assert "parse failed: bad xref" in screen_text(app)

    async def test_an_empty_error_message_counts_as_none(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [step(_detail(error_message=""))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert not app.screen.query_one("#detail-error").display

    async def test_a_huge_error_message_is_capped(self, stub_client, step) -> None:
        stub_client.detail_script = [step(_detail(error_message="x" * 200_000))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert len(screen.values["error"]) <= 5_000
            assert screen.values["error"].endswith("…")


class TestErrors:
    @pytest.mark.parametrize(
        "error",
        [
            ConnectionFailed("Cannot reach the Grimoire API at http://x - is it up?"),
            TimedOut("The request timed out."),
            AuthFailed("API key rejected. Check GRIMOIRE_API_KEY."),
            RateLimited("Rate limited. Retry in 30s.", retry_after=30),
            ServerError("Grimoire API error - check the API logs."),
            MalformedResponse("Unexpected response from the API."),
            RequestRejected("Not found: Document nope not found"),
        ],
    )
    async def test_each_client_error_shows_its_message_and_keeps_the_modal_open(
        self, stub_client, step, error
    ) -> None:
        stub_client.detail_script = [step(error)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert app.is_running
            assert app.screen is screen  # still open, so it can be read
            assert screen.status_text == error.message
            assert screen.values["title"] == "-"

    async def test_a_404_says_not_found(self, stub_client, step) -> None:
        stub_client.detail_script = [step(RequestRejected("Not found: gone"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert "Not found" in screen.status_text

    async def test_an_unexpected_exception_shows_only_the_generic_message(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [step(RuntimeError("secret /srv/app/db.py line 9"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert screen.status_text == GENERIC_ERROR
            assert "secret" not in screen_text(app)

    async def test_a_hostile_error_message_cannot_break_rendering(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [
            step(RequestRejected("bad [/nonexistent] [bold red]x"))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 40)) as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert app.is_running
            assert "[/nonexistent]" in screen_text(app)


class TestConnectionReporting:
    async def test_a_connection_failure_marks_the_api_unreachable(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [step(ConnectionFailed("Cannot reach the API."))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _launched(app, pilot)
            assert _base_bar(app).state == "connected"

            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert _base_bar(app).state == "unreachable"

    async def test_a_success_marks_the_api_connected(self, stub_client, step) -> None:
        stub_client.healthy = False
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _launched(app, pilot)
            await _until(pilot, lambda: _base_bar(app).state == "unreachable")

            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert _base_bar(app).state == "connected"

    async def test_a_404_still_means_the_server_is_up(self, stub_client, step) -> None:
        stub_client.healthy = False
        stub_client.detail_script = [step(RequestRejected("Not found: gone"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _launched(app, pilot)
            await _until(pilot, lambda: _base_bar(app).state == "unreachable")

            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert _base_bar(app).state == "connected"


class TestClosing:
    @pytest.mark.parametrize("key", ["escape", "q"])
    async def test_a_key_closes_the_modal(self, stub_client, step, key: str) -> None:
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)
            assert len(app.screen_stack) == 2

            await pilot.press(key)
            await pilot.pause()

            assert len(app.screen_stack) == 1

    async def test_the_close_button_closes_the_modal(self, stub_client, step) -> None:
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            await pilot.click("#detail-close")
            await pilot.pause()

            assert len(app.screen_stack) == 1

    async def test_the_close_button_has_the_keyboard_so_enter_closes(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            assert app.focused is app.screen.query_one("#detail-close", Button)

    async def test_an_error_does_not_close_the_modal_by_itself(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [step(ServerError("boom"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)
            for _ in range(3):
                await pilot.pause()

            assert len(app.screen_stack) == 2

    async def test_closing_mid_fetch_neither_raises_nor_leaks_the_late_result(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.detail_script = [step(_detail(), gate=gate)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _launched(app, pilot)
            before = _base_bar(app).state
            await _show(app, pilot, stub_client)

            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1

            gate.set()  # the abandoned fetch now returns
            await _settled(app, pilot)

            assert app.is_running
            assert len(app.screen_stack) == 1
            assert _base_bar(app).state == before

    async def test_closing_mid_fetch_then_reopening_shows_the_new_document_only(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.detail_script = [
            step(_detail(id="OLD", title="old doc"), gate=gate),
            step(_detail(id="doc-2", title="new doc")),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client, "OLD")
            await pilot.press("escape")
            await pilot.pause()

            reopened = DocumentDetailScreen(stub_client, "doc-2")  # type: ignore[arg-type]
            await app.push_screen(reopened)
            await _until(pilot, lambda: len(stub_client.calls_to("get_document")) >= 2)
            gate.set()
            await _settled(app, pilot)

            assert reopened.values["title"] == "new doc"


class TestUntrustedText:
    HOSTILE = "[bold red]boom[/] [link=http://evil.example]x[/link] [/nope]"

    async def test_hostile_values_render_literally_in_every_field(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [
            step(
                _detail(
                    id=self.HOSTILE,
                    title=self.HOSTILE,
                    source_path=self.HOSTILE,
                    file_type=self.HOSTILE,
                    storage_backend=self.HOSTILE,
                    processing_status=self.HOSTILE,
                    error_message=self.HOSTILE,
                )
            )
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(200, 60)) as pilot:
            screen = await _show(app, pilot, stub_client, "doc-1")
            await _settled(app, pilot)

            assert app.is_running
            for key in (
                "id",
                "title",
                "source_path",
                "file_type",
                "storage_backend",
                "processing_status",
                "error",
            ):
                assert screen.values[key] == self.HOSTILE, key
            assert screen_text(app).count("[bold red]boom[/]") >= 4

    async def test_every_value_widget_is_created_with_markup_off(
        self, stub_client, step
    ) -> None:
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot, stub_client)
            await _settled(app, pilot)

            cells = list(app.screen.query(".detail-value").results(Static))
            assert len(cells) == 12  # one per field, tags and chunks included
            assert all(cell._render_markup is False for cell in cells)
