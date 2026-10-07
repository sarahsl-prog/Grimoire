"""The Watch pane: which directories the server is watching, and a way to change that.

A watch ingests new and changed files from a directory on the machine running the
API.  The pane lists the active watches with the watcher's running counters
(``GET /watch/status`` works with any key); ``n`` starts one and ``x`` stops the
highlighted one, both of which need a ``dvl`` or ``agt`` key.  A server started
without ``--watch`` answers 503; that is shown as a status line, not a crash.

As elsewhere, the client is synchronous and a thread cannot be killed, so every
load carries an id and a response for an older one is dropped.  Paths come from
the server and may contain anything, so cells are Rich ``Text``, never markup.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, DataTable, Static
from textual.worker import get_current_worker

from grimoire.api.schemas import WatcherStatsResponse, WatchResponse
from grimoire.client.client import GrimoireClient
from grimoire.tui.errors import reachability, user_message
from grimoire.tui.formatting import truncate
from grimoire.tui.messages import ConnectionReport
from grimoire.tui.screens.new_watch import NewWatchScreen

_LOADING = "Loading…"
_NO_ROWS = "Nothing is being watched. Press n to watch a directory."
_BACKEND_WIDTH = 8
_STATE_WIDTH = 8
_GAP = 1  # DataTable pads each cell by one on each side; widths are content widths
_MIN_PATH = 12


class WatchPane(Vertical):
    """List active watches; start and stop them.

    Args:
        client: API client.  Owned by the caller.
        id: Widget id.

    Attributes:
        status_text: Plain text of the status line (loading or an error).
        summary_text: Plain text of the counters line under the table.
    """

    BINDINGS = [
        Binding("n", "new_watch", "Watch a directory"),
        Binding("x", "stop_watch", "Stop watching"),
    ]

    class Loaded(Message):
        """The status arrived."""

        def __init__(self, request_id: int, result: WatcherStatsResponse) -> None:
            super().__init__()
            self.request_id = request_id
            self.result = result

    class Failed(Message):
        """The status request raised."""

        def __init__(self, request_id: int, error: BaseException) -> None:
            super().__init__()
            self.request_id = request_id
            self.error = error

    class Stopped(Message):
        """The server stopped a watch."""

        def __init__(self, path: str) -> None:
            super().__init__()
            self.path = path

    class StopFailed(Message):
        """The stop call raised."""

        def __init__(self, error: BaseException) -> None:
            super().__init__()
            self.error = error

    def __init__(self, client: GrimoireClient, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._client = client
        self._request_id = 0
        self._has_data = False
        self._in_flight = False
        self._stopping = False
        self._watches: list[WatchResponse] = []
        self.status_text = ""
        self.summary_text = ""

    def set_client(self, client: GrimoireClient) -> None:
        """Use a different client from now on (the API key was replaced)."""
        self._client = client

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="watch-toolbar"):
            yield Button("Watch a directory", id="watch-new", variant="primary")
            yield Button("Stop", id="watch-stop")
            yield Button("Refresh", id="watch-refresh")
        yield Static("", id="watch-status", markup=False)
        yield DataTable(id="watch-table", cursor_type="row", zebra_stripes=True)
        yield Static(_NO_ROWS, id="watch-empty", markup=False)
        yield Static("", id="watch-summary", markup=False)

    def on_mount(self) -> None:
        self.query_one("#watch-empty").display = False

    def on_resize(self, event: events.Resize) -> None:
        # The table's own width settles after the pane's.
        self.call_after_refresh(self._relayout)

    # -- hooks called by the app ---------------------------------------------

    def tab_shown(self) -> None:
        """First time the tab is shown, load.  A failed first load retries here."""
        if not self._in_flight and not self._has_data:
            self._load()

    def focus_primary(self) -> None:
        table = self.query_one("#watch-table", DataTable)
        if table.display:
            table.focus()
        else:
            self.query_one("#watch-new", Button).focus()

    def refresh_data(self) -> None:
        self._load()

    # -- actions --------------------------------------------------------------

    def action_new_watch(self) -> None:
        self.app.push_screen(NewWatchScreen(self._client), self._on_started)

    def _on_started(self, started: WatchResponse | None) -> None:
        if started is None:
            return
        self.app.notify(f"Watching {started.path}", markup=False)
        self._load()

    def action_stop_watch(self) -> None:
        if self._stopping or not self._watches:
            return
        table = self.query_one("#watch-table", DataTable)
        row = table.cursor_row
        if not 0 <= row < len(self._watches):
            return
        self._stopping = True
        self._stop(self._watches[row])

    @on(Button.Pressed, "#watch-new")
    def _on_new_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_new_watch()

    @on(Button.Pressed, "#watch-stop")
    def _on_stop_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_stop_watch()

    @on(Button.Pressed, "#watch-refresh")
    def _on_refresh_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.refresh_data()

    @work(thread=True, exit_on_error=False, group="watch-stop")
    def _stop(self, watch: WatchResponse) -> None:
        worker = get_current_worker()
        message: Message
        try:
            self._client.stop_watch(watch.watch_id)
            message = self.Stopped(watch.path)
        except Exception as exc:
            message = self.StopFailed(exc)
        if not worker.is_cancelled:
            self.post_message(message)

    def on_watch_pane_stopped(self, message: Stopped) -> None:
        self._stopping = False
        self.app.notify(f"Stopped watching {message.path}", markup=False)
        self._load()

    def on_watch_pane_stop_failed(self, message: StopFailed) -> None:
        self._stopping = False
        text = user_message(message.error)
        self.app.notify(text, severity="error", markup=False)
        reachable = reachability(message.error)
        if reachable is not None:
            self.post_message(ConnectionReport(reachable=reachable))

    # -- loading --------------------------------------------------------------

    def _load(self) -> None:
        self._request_id += 1
        self._in_flight = True
        self._set_status(_LOADING)
        self._fetch(self._request_id)

    @work(thread=True, exclusive=True, group="watch", exit_on_error=False)
    def _fetch(self, request_id: int) -> None:
        worker = get_current_worker()
        message: Message
        try:
            message = self.Loaded(request_id, self._client.watch_status())
        except Exception as exc:
            message = self.Failed(request_id, exc)
        if not worker.is_cancelled:
            self.post_message(message)

    def on_watch_pane_loaded(self, message: Loaded) -> None:
        if message.request_id != self._request_id:
            return  # a newer request is on its way
        self._in_flight = False
        self._has_data = True
        self._watches = list(message.result.watches)
        self._set_status("")
        self._show_rows(message.result)
        self.post_message(ConnectionReport(reachable=True))

    def on_watch_pane_failed(self, message: Failed) -> None:
        if message.request_id != self._request_id:
            return
        self._in_flight = False
        text = user_message(message.error)
        self._set_status(text, error=True)
        reachable = reachability(message.error)
        if reachable is not None:
            self.post_message(ConnectionReport(reachable=reachable))

    # -- rendering ------------------------------------------------------------

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = text
        line = self.query_one("#watch-status", Static)
        line.update(text)
        line.set_class(error, "-error")

    def _show_rows(self, stats: WatcherStatsResponse) -> None:
        table = self.query_one("#watch-table", DataTable)
        self._fill_table(table)
        has_rows = bool(self._watches)
        had_focus = table.has_focus
        table.display = has_rows
        self.query_one("#watch-empty").display = not has_rows
        if not has_rows and had_focus:
            # Hiding the focused table drops focus; keep the keyboard somewhere.
            self.query_one("#watch-new", Button).focus()
        self.summary_text = (
            f"{stats.active_watches} active · "
            f"{stats.total_files_processed} files processed · "
            f"{stats.total_files_failed} failed"
        )
        self.query_one("#watch-summary", Static).update(self.summary_text)

    def _fill_table(self, table: DataTable[Any]) -> None:
        available = max(0, table.size.width - table.styles.scrollbar_size_vertical)
        used = _BACKEND_WIDTH + _STATE_WIDTH + 3 * (2 * _GAP)
        path_w = max(_MIN_PATH, available - used)
        previous = table.cursor_row
        table.clear(columns=True)
        # Explicit widths, so the table does not measure its own cells.
        table.add_column("Path", width=path_w)
        table.add_column("Backend", width=_BACKEND_WIDTH)
        table.add_column("State", width=_STATE_WIDTH)
        for watch in self._watches:
            table.add_row(
                Text(truncate(watch.path, path_w)),
                Text(truncate(watch.backend, _BACKEND_WIDTH)),
                Text("running" if watch.is_running else "stopped"),
            )
        if self._watches:
            table.move_cursor(row=min(previous, len(self._watches) - 1))

    def _relayout(self) -> None:
        if self._watches:
            self._fill_table(self.query_one("#watch-table", DataTable))
