"""Tests for the Ingest pane.

Real files in a temp directory, a stub client whose ``upload`` can be held in
flight or made to raise. The risky properties: uploads must be serial, one
failure must not stop the rest, nothing typed or returned may be parsed as
markup, and a successful ingest must make the other tabs reload.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from textual.widgets import Checkbox, DataTable, Input, TabbedContent  # noqa: E402

from grimoire.api.schemas import IngestResultResponse  # noqa: E402
from grimoire.client.errors import ConnectionFailed, RequestRejected  # noqa: E402
from grimoire.tui.app import GrimoireApp  # noqa: E402
from grimoire.tui.errors import GENERIC_ERROR  # noqa: E402
from grimoire.tui.widgets.ingest_pane import IngestPane, normalize_path  # noqa: E402

from .test_categories_pane import _cat, _list  # noqa: E402
from .test_documents_pane import _launched, _page, _settled, _until  # noqa: E402


def _result(status: str = "completed", **fields: Any) -> IngestResultResponse:
    base: dict[str, Any] = {
        "file_path": "/staged/x",
        "status": status,
        "chunks_created": 3,
        "duration_ms": 120,
    }
    base.update(fields)
    return IngestResultResponse(**base)


def _file(tmp_path: Path, name: str = "report.pdf") -> Path:
    path = tmp_path / name
    path.write_text("content")
    return path


def _pane(app: GrimoireApp) -> IngestPane:
    return app.query_one(IngestPane)


def _table(app: GrimoireApp) -> DataTable[Any]:
    return app.query_one("#ingest-table", DataTable)


def _rows(app: GrimoireApp) -> list[list[str]]:
    table = _table(app)
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


async def _show(app: GrimoireApp, pilot: Any) -> None:
    await _launched(app, pilot)
    await pilot.press("f4")
    await _until(pilot, lambda: app.query_one(TabbedContent).active == "ingest")
    await _settled(app, pilot)


async def _ingest(app: GrimoireApp, pilot: Any, text: str) -> None:
    box = app.query_one("#ingest-path", Input)
    box.focus()
    await pilot.pause()
    box.value = text
    await pilot.press("enter")


class TestNormalizePath:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/a/b.pdf", Path("/a/b.pdf")),
            ("  /a/b.pdf  ", Path("/a/b.pdf")),
            ('"/a/b c.pdf"', Path("/a/b c.pdf")),
            ("'/a/b c.pdf'", Path("/a/b c.pdf")),
            ('  " /a/b.pdf "  ', Path("/a/b.pdf")),
            ("~/x.md", Path("~/x.md").expanduser()),
        ],
    )
    def test_cleans_what_shells_and_file_managers_paste(
        self, text: str, expected: Path
    ) -> None:
        assert normalize_path(text) == expected

    @pytest.mark.parametrize("text", ["", "   ", '""', "''", '" "'])
    def test_nothing_typed_is_none(self, text: str) -> None:
        assert normalize_path(text) is None

    def test_a_lone_quote_is_part_of_the_name_not_stripped(self) -> None:
        assert normalize_path('"/a/b.pdf') == Path('"/a/b.pdf')


class TestUploading:
    async def test_enter_uploads_the_file_and_shows_the_outcome(
        self, stub_client, step, tmp_path
    ) -> None:
        path = _file(tmp_path)
        stub_client.upload_script = [step(_result())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(path))
            await _until(
                pilot, lambda: _table(app).row_count == 1 and _rows(app)[0][1] == "done"
            )

            assert _rows(app)[0] == ["report.pdf", "done", "3 chunks · 120 ms"]
            assert app.query_one("#ingest-path", Input).value == ""
        ((args, kwargs),) = stub_client.calls_to("upload")
        assert args == (path,)
        assert kwargs == {"auto_tag": True}

    async def test_unchecking_auto_tag_is_sent(
        self, stub_client, step, tmp_path
    ) -> None:
        stub_client.upload_script = [step(_result())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            app.query_one("#ingest-autotag", Checkbox).value = False
            await _ingest(app, pilot, str(_file(tmp_path)))
            await _until(pilot, lambda: len(stub_client.calls_to("upload")) == 1)

        assert stub_client.calls_to("upload")[0][1] == {"auto_tag": False}

    async def test_a_quoted_path_is_cleaned_before_uploading(
        self, stub_client, step, tmp_path
    ) -> None:
        path = _file(tmp_path, "with space.md")
        stub_client.upload_script = [step(_result())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, f'"{path}"')
            await _until(pilot, lambda: len(stub_client.calls_to("upload")) == 1)

        assert stub_client.calls_to("upload")[0][0] == (path,)

    async def test_a_skipped_file_says_it_is_already_there(
        self, stub_client, step, tmp_path
    ) -> None:
        stub_client.upload_script = [step(_result("skipped"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(_file(tmp_path)))
            await _until(
                pilot,
                lambda: _table(app).row_count == 1 and _rows(app)[0][1] == "skipped",
            )

            assert _rows(app)[0][2] == "Already in the corpus"

    async def test_a_failed_ingest_shows_the_servers_reason(
        self, stub_client, step, tmp_path
    ) -> None:
        stub_client.upload_script = [
            step(_result("failed", error_message="Could not parse the PDF"))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(_file(tmp_path)))
            await _until(
                pilot,
                lambda: _table(app).row_count == 1 and _rows(app)[0][1] == "failed",
            )

            assert _rows(app)[0][2] == "Could not parse the PDF"

    async def test_a_refused_upload_shows_why(
        self, stub_client, step, tmp_path
    ) -> None:
        stub_client.upload_script = [
            step(RequestRejected("This API key is not permitted to do that: needs dvl"))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(_file(tmp_path)))
            await _until(
                pilot,
                lambda: _table(app).row_count == 1 and _rows(app)[0][1] == "failed",
            )

            assert "needs dvl" in _rows(app)[0][2]

    async def test_an_unexpected_error_shows_the_generic_line(
        self, stub_client, step, tmp_path
    ) -> None:
        stub_client.upload_script = [step(RuntimeError("secret /srv/path"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(140, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(_file(tmp_path)))
            await _until(
                pilot,
                lambda: _table(app).row_count == 1 and _rows(app)[0][1] == "failed",
            )

            assert GENERIC_ERROR.startswith(_rows(app)[0][2].rstrip("…"))
            assert "secret" not in _rows(app)[0][2]


class TestRefusedLocally:
    @pytest.mark.parametrize("text", ["", "   "])
    async def test_nothing_typed(self, stub_client, step, text) -> None:
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, text)
            await pilot.pause()

            assert "Enter the path" in _pane(app).status_text
        assert stub_client.calls_to("upload") == []

    async def test_a_directory(self, stub_client, step, tmp_path) -> None:
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(tmp_path))
            await pilot.pause()

            assert "directory" in _pane(app).status_text
        assert stub_client.calls_to("upload") == []

    async def test_a_missing_file(self, stub_client, step, tmp_path) -> None:
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(tmp_path / "nope.pdf"))
            await pilot.pause()

            assert "No such file" in _pane(app).status_text
        assert stub_client.calls_to("upload") == []

    async def test_a_refusal_does_not_clear_what_was_typed(
        self, stub_client, step, tmp_path
    ) -> None:
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _show(app, pilot)
            typo = str(tmp_path / "nope.pdf")
            await _ingest(app, pilot, typo)
            await pilot.pause()

            assert app.query_one("#ingest-path", Input).value == typo


class TestQueue:
    async def test_uploads_run_one_at_a_time_in_order(
        self, stub_client, step, tmp_path
    ) -> None:
        gate = threading.Event()
        first, second = _file(tmp_path, "a.pdf"), _file(tmp_path, "b.pdf")
        stub_client.upload_script = [step(_result(), gate=gate), step(_result())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(first))
            await _until(pilot, lambda: len(stub_client.calls_to("upload")) == 1)
            await _ingest(app, pilot, str(second))
            await _until(pilot, lambda: _table(app).row_count == 2)
            for _ in range(6):
                await pilot.pause()

            # The second is waiting, not started: the first is still held.
            assert [r[1] for r in _rows(app)] == ["uploading", "queued"]
            assert len(stub_client.calls_to("upload")) == 1

            gate.set()
            await _until(pilot, lambda: [r[1] for r in _rows(app)] == ["done", "done"])
        assert [a[0] for a, _ in stub_client.calls_to("upload")] == [first, second]

    async def test_one_failure_does_not_stop_the_rest(
        self, stub_client, step, tmp_path
    ) -> None:
        first, second = _file(tmp_path, "a.pdf"), _file(tmp_path, "b.pdf")
        stub_client.upload_script = [
            step(ConnectionFailed("Cannot reach the API.")),
            step(_result()),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(first))
            await _ingest(app, pilot, str(second))
            await _until(
                pilot, lambda: [r[1] for r in _rows(app)] == ["failed", "done"]
            )

    async def test_the_footer_summarises_the_outcomes(
        self, stub_client, step, tmp_path
    ) -> None:
        stub_client.upload_script = [step(_result()), step(_result("skipped"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(_file(tmp_path, "a.pdf")))
            await _ingest(app, pilot, str(_file(tmp_path, "b.pdf")))
            await _until(pilot, lambda: _pane(app).footer_text == "1 done · 1 skipped")


class TestUntrustedText:
    async def test_markup_in_a_file_name_is_shown_literally(
        self, stub_client, step, tmp_path
    ) -> None:
        stub_client.upload_script = [step(_result())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(_file(tmp_path, "[bold red]x[b].md")))
            await _until(pilot, lambda: _table(app).row_count == 1)

            assert _rows(app)[0][0] == "[bold red]x[b].md"

    async def test_markup_in_a_server_message_is_shown_literally(
        self, stub_client, step, tmp_path
    ) -> None:
        stub_client.upload_script = [
            step(_result("failed", error_message="[bold red]oops[/]"))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _show(app, pilot)
            await _ingest(app, pilot, str(_file(tmp_path)))
            await _until(
                pilot,
                lambda: _table(app).row_count == 1 and _rows(app)[0][1] == "failed",
            )

            assert _rows(app)[0][2] == "[bold red]oops[/]"


class TestOtherTabsReload:
    async def _loaded_both(
        self, app: GrimoireApp, pilot: Any, stub_client: Any
    ) -> None:
        """Visit Documents and Categories once so both hold data."""
        await _launched(app, pilot)
        await pilot.press("f2")
        await _until(pilot, lambda: len(stub_client.calls_to("list_documents")) == 1)
        await _settled(app, pilot)
        await pilot.press("f3")
        await _until(pilot, lambda: len(stub_client.calls_to("list_categories")) == 1)
        await _settled(app, pilot)

    async def test_a_successful_ingest_makes_documents_and_categories_reload(
        self, stub_client, step, tmp_path
    ) -> None:
        stub_client.documents_script = [step(_page(1)), step(_page(2))]
        stub_client.categories_script = [step(_list(_cat(1))), step(_list(_cat(1)))]
        stub_client.upload_script = [step(_result())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await self._loaded_both(app, pilot, stub_client)
            await pilot.press("f4")
            await _until(pilot, lambda: app.query_one(TabbedContent).active == "ingest")
            await _settled(app, pilot)
            await _ingest(app, pilot, str(_file(tmp_path)))
            await _until(
                pilot, lambda: _table(app).row_count == 1 and _rows(app)[0][1] == "done"
            )

            await pilot.press("f2")
            await _until(
                pilot, lambda: len(stub_client.calls_to("list_documents")) == 2
            )
            await pilot.press("f3")
            await _until(
                pilot, lambda: len(stub_client.calls_to("list_categories")) == 2
            )

    async def test_a_failed_or_skipped_ingest_does_not_trigger_a_reload(
        self, stub_client, step, tmp_path
    ) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.categories_script = [step(_list(_cat(1)))]
        stub_client.upload_script = [step(_result("skipped")), step(_result("failed"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await self._loaded_both(app, pilot, stub_client)
            await pilot.press("f4")
            await _until(pilot, lambda: app.query_one(TabbedContent).active == "ingest")
            await _settled(app, pilot)
            await _ingest(app, pilot, str(_file(tmp_path, "a.pdf")))
            await _ingest(app, pilot, str(_file(tmp_path, "b.pdf")))
            await _until(
                pilot, lambda: [r[1] for r in _rows(app)] == ["skipped", "failed"]
            )
            await pilot.press("f2")
            await _settled(app, pilot)
            for _ in range(6):
                await pilot.pause()

        assert len(stub_client.calls_to("list_documents")) == 1
        assert len(stub_client.calls_to("list_categories")) == 1


async def test_f4_shows_the_ingest_tab_and_focuses_the_box(stub_client, step) -> None:
    app = GrimoireApp(stub_client, stub_client.config, None)
    async with app.run_test() as pilot:
        await _show(app, pilot)
        await _until(pilot, lambda: app.focused is app.query_one("#ingest-path", Input))
