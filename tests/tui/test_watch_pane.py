"""Tests for the Watch pane and the start-watch modal.

The client is a stub whose calls can be scripted, held in flight, or made to
raise.  The risky properties: a stop acts on the highlighted row only, a second
press while a call is out does nothing, a server without a watcher is a status
line (not a crash), and server-supplied paths render literally.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from textual.widgets import Button, Checkbox, DataTable, Input, TabbedContent  # noqa: E402

from grimoire.api.schemas import WatcherStatsResponse, WatchResponse  # noqa: E402
from grimoire.client.errors import RequestRejected, ServerError  # noqa: E402
from grimoire.tui.app import GrimoireApp  # noqa: E402
from grimoire.tui.errors import GENERIC_ERROR  # noqa: E402
from grimoire.tui.screens.new_watch import NewWatchScreen  # noqa: E402
from grimoire.tui.widgets.watch_pane import WatchPane  # noqa: E402

from .test_documents_pane import _launched, _settled, _until  # noqa: E402


def _watch(i: int, **overrides: Any) -> WatchResponse:
    fields: dict[str, Any] = {
        "watch_id": f"w{i}",
        "path": f"/tmp/dir{i}",
        "backend": "local",
        "is_running": True,
    }
    fields.update(overrides)
    return WatchResponse(**fields)


def _stats(*watches: WatchResponse, processed: int = 0, failed: int = 0) -> Any:
    return WatcherStatsResponse(
        active_watches=len(watches),
        total_files_processed=processed,
        total_files_failed=failed,
        watches=list(watches),
    )


def _pane(app: GrimoireApp) -> WatchPane:
    return app.query_one(WatchPane)


def _table(app: GrimoireApp) -> DataTable[Any]:
    return app.query_one("#watch-table", DataTable)


async def _open(app: GrimoireApp, pilot: Any) -> None:
    await _launched(app, pilot)
    await pilot.press("f5")
    await _until(pilot, lambda: app.query_one(TabbedContent).active == "watch")
    await _settled(app, pilot)


def _cells(app: GrimoireApp) -> list[list[str]]:
    table = _table(app)
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


class TestLoading:
    async def test_f5_shows_the_tab_and_loads_once(self, stub_client, step) -> None:
        stub_client.watch_status_script = [step(_stats(_watch(1), _watch(2)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert len(stub_client.calls_to("watch_status")) == 1
            assert _table(app).row_count == 2

    async def test_rows_and_counters(self, stub_client, step) -> None:
        stub_client.watch_status_script = [
            step(_stats(_watch(1), processed=14, failed=1))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(app, pilot)

            assert _cells(app) == [["/tmp/dir1", "local", "running"]]
            assert _pane(app).summary_text == (
                "1 active · 14 files processed · 1 failed"
            )

    async def test_nothing_watched_says_how_to_start(self, stub_client, step) -> None:
        stub_client.watch_status_script = [step(_stats())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert app.query_one("#watch-empty").display is True
            assert _table(app).display is False

    async def test_a_server_without_a_watcher_is_a_status_line(
        self, stub_client, step
    ) -> None:
        stub_client.watch_status_script = [
            step(ServerError("The API is not ready: Watcher not initialized."))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert "not initialized" in _pane(app).status_text

    async def test_an_unexpected_error_shows_the_generic_line(
        self, stub_client, step
    ) -> None:
        stub_client.watch_status_script = [step(RuntimeError("SELECT secret"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert _pane(app).status_text == GENERIC_ERROR

    async def test_a_path_is_shown_literally_not_as_markup(
        self, stub_client, step
    ) -> None:
        stub_client.watch_status_script = [
            step(_stats(_watch(1, path="/tmp/[bold red]x[/]")))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(app, pilot)

            assert _cells(app)[0][0] == "/tmp/[bold red]x[/]"


class TestStopping:
    async def test_x_stops_the_highlighted_watch_and_reloads(
        self, stub_client, step
    ) -> None:
        stub_client.watch_status_script = [
            step(_stats(_watch(1), _watch(2))),
            step(_stats(_watch(1))),
        ]
        stub_client.stop_watch_script = [step(None)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(app, pilot)
            _table(app).focus()
            await pilot.press("down", "x")
            await _until(pilot, lambda: _table(app).row_count == 1)

        assert stub_client.calls_to("stop_watch") == [(("w2",), {})]

    async def test_a_second_x_while_stopping_does_nothing(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.watch_status_script = [step(_stats(_watch(1))), step(_stats())]
        stub_client.stop_watch_script = [step(None, gate=gate)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(app, pilot)
            _table(app).focus()
            await pilot.press("x")
            await _until(pilot, lambda: len(stub_client.calls_to("stop_watch")) == 1)
            await pilot.press("x")
            await pilot.pause()
            gate.set()
            await _until(pilot, lambda: _table(app).display is False)

        assert len(stub_client.calls_to("stop_watch")) == 1

    async def test_a_refused_stop_leaves_the_list_and_can_be_retried(
        self, stub_client, step
    ) -> None:
        stub_client.watch_status_script = [step(_stats(_watch(1))), step(_stats())]
        stub_client.stop_watch_script = [
            step(RequestRejected("This API key is not permitted to do that: dvl")),
            step(None),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(app, pilot)
            _table(app).focus()
            await pilot.press("x")
            await _until(pilot, lambda: len(stub_client.calls_to("stop_watch")) == 1)
            await _settled(app, pilot)
            assert _table(app).row_count == 1  # unchanged

            await pilot.press("x")
            await _until(pilot, lambda: _table(app).display is False)

        assert len(stub_client.calls_to("stop_watch")) == 2

    async def test_x_with_nothing_listed_does_nothing(self, stub_client, step) -> None:
        stub_client.watch_status_script = [step(_stats())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("x")
            await pilot.pause()

        assert stub_client.calls_to("stop_watch") == []


class TestStartWatch:
    async def _modal(self, app: GrimoireApp, pilot: Any) -> NewWatchScreen:
        await pilot.press("n")
        await _until(pilot, lambda: isinstance(app.screen, NewWatchScreen))
        return app.screen  # type: ignore[return-value]

    async def test_n_opens_the_modal_and_focuses_the_path(
        self, stub_client, step
    ) -> None:
        stub_client.watch_status_script = [step(_stats())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)

            assert app.focused is screen.query_one("#newwatch-path", Input)

    async def test_starting_sends_the_path_and_reloads(self, stub_client, step) -> None:
        stub_client.watch_status_script = [step(_stats()), step(_stats(_watch(1)))]
        stub_client.start_watch_script = [step(_watch(1))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            screen.query_one("#newwatch-path", Input).value = "  /tmp/dir1  "
            await pilot.press("enter")
            await _until(pilot, lambda: _table(app).row_count == 1)

            assert not isinstance(app.screen, NewWatchScreen)
        assert stub_client.calls_to("start_watch") == [
            (("/tmp/dir1",), {"recursive": True})
        ]

    async def test_the_recursive_box_is_honoured(self, stub_client, step) -> None:
        stub_client.watch_status_script = [step(_stats()), step(_stats(_watch(1)))]
        stub_client.start_watch_script = [step(_watch(1))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            screen.query_one("#newwatch-path", Input).value = "/tmp/dir1"
            screen.query_one("#newwatch-recursive", Checkbox).value = False
            await pilot.click("#newwatch-start")
            await _until(pilot, lambda: _table(app).row_count == 1)

        assert stub_client.calls_to("start_watch")[0][1] == {"recursive": False}

    async def test_a_blank_path_is_refused_without_calling_the_api(
        self, stub_client, step
    ) -> None:
        stub_client.watch_status_script = [step(_stats())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            await pilot.press("enter")
            await pilot.pause()

            assert "Enter the directory" in screen.error_text
            assert isinstance(app.screen, NewWatchScreen)
        assert stub_client.calls_to("start_watch") == []

    async def test_a_refusal_stays_open_and_says_why(self, stub_client, step) -> None:
        stub_client.watch_status_script = [step(_stats())]
        stub_client.start_watch_script = [
            step(RequestRejected("Path is not in an allowed directory. Allowed: /tmp."))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            screen.query_one("#newwatch-path", Input).value = "/etc"
            await pilot.press("enter")
            await _until(pilot, lambda: screen.error_text != "")

            assert "allowed directory" in screen.error_text
            assert isinstance(app.screen, NewWatchScreen)
            assert screen.query_one("#newwatch-start", Button).disabled is False

    async def test_a_second_enter_while_starting_does_not_start_twice(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.watch_status_script = [step(_stats()), step(_stats(_watch(1)))]
        stub_client.start_watch_script = [step(_watch(1), gate=gate)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            screen.query_one("#newwatch-path", Input).value = "/tmp/dir1"
            await pilot.press("enter")
            await _until(pilot, lambda: len(stub_client.calls_to("start_watch")) == 1)
            await pilot.press("enter")
            await pilot.pause()
            gate.set()
            await _until(pilot, lambda: not isinstance(app.screen, NewWatchScreen))

        assert len(stub_client.calls_to("start_watch")) == 1

    async def test_escape_cancels_without_calling_the_api(
        self, stub_client, step
    ) -> None:
        stub_client.watch_status_script = [step(_stats())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await self._modal(app, pilot)
            await pilot.press("escape")
            await _until(pilot, lambda: not isinstance(app.screen, NewWatchScreen))

        assert stub_client.calls_to("start_watch") == []
