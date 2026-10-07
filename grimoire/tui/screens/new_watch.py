"""A modal for starting a watch on a directory of the machine running the API.

The path is a path *on the server*, not on the machine running the TUI, and the
server only accepts directories under its ``api.allowed_roots``; a refusal is
shown inside the modal, which stays open so the path can be corrected.  While the
call runs the buttons are disabled so one ``Enter`` cannot start the watch twice.

Server text (the refusal) is displayed with markup off.
"""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Static
from textual.worker import get_current_worker

from grimoire.api.schemas import WatchResponse
from grimoire.client.client import GrimoireClient
from grimoire.tui.errors import user_message

_PATH_REQUIRED = "Enter the directory to watch."


class NewWatchScreen(ModalScreen[WatchResponse | None]):
    """Ask for a directory to watch; dismiss with the new watch, or ``None``.

    Args:
        client: API client.  Owned by the caller.

    Attributes:
        error_text: Plain text of the inline error line ("" when none).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    class Started(Message):
        """The server started the watch."""

        def __init__(self, watch: WatchResponse) -> None:
            super().__init__()
            self.watch = watch

    class Failed(Message):
        """The start call raised."""

        def __init__(self, error: BaseException) -> None:
            super().__init__()
            self.error = error

    def __init__(self, client: GrimoireClient) -> None:
        super().__init__()
        self._client = client
        self._busy = False
        self.error_text = ""

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="newwatch-box"):
            yield Static("Watch a directory", id="newwatch-heading", markup=False)
            yield Static(
                "A path on the machine running the API, under its allowed roots.",
                id="newwatch-hint",
                markup=False,
            )
            yield Input(placeholder="/path/to/directory", id="newwatch-path")
            yield Checkbox("Include subdirectories", True, id="newwatch-recursive")
            yield Static("", id="newwatch-error", markup=False)
            with Horizontal(id="newwatch-buttons"):
                yield Button("Watch", id="newwatch-start", variant="primary")
                yield Button("Cancel", id="newwatch-cancel")

    def on_mount(self) -> None:
        self.query_one("#newwatch-error").display = False
        self.query_one("#newwatch-path", Input).focus()

    # -- state ----------------------------------------------------------------

    def _set_error(self, text: str) -> None:
        self.error_text = text
        line = self.query_one("#newwatch-error", Static)
        line.update(text)
        line.display = bool(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button_id in ("#newwatch-start", "#newwatch-cancel"):
            self.query_one(button_id, Button).disabled = busy

    # -- actions --------------------------------------------------------------

    def action_cancel(self) -> None:
        # Not while the call is out: dismissing now would drop its result and
        # leave the person unsure whether the watch exists.
        if not self._busy:
            self.dismiss(None)

    @on(Button.Pressed, "#newwatch-cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    @on(Button.Pressed, "#newwatch-start")
    def _on_start_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._submit()

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def _submit(self) -> None:
        if self._busy:
            return
        path = self.query_one("#newwatch-path", Input).value.strip()
        if not path:
            self._set_error(_PATH_REQUIRED)
            return
        recursive = self.query_one("#newwatch-recursive", Checkbox).value
        self._set_error("")
        self._set_busy(True)
        self._start(path, recursive)

    @work(thread=True, exit_on_error=False)
    def _start(self, path: str, recursive: bool) -> None:
        """Call the API off the event loop and post the outcome."""
        worker = get_current_worker()
        message: Message
        try:
            message = self.Started(self._client.start_watch(path, recursive=recursive))
        except Exception as exc:
            message = self.Failed(exc)
        if not worker.is_cancelled:
            self.post_message(message)

    def on_new_watch_screen_started(self, message: Started) -> None:
        self._set_busy(False)
        self.dismiss(message.watch)

    def on_new_watch_screen_failed(self, message: Failed) -> None:
        self._set_busy(False)
        self._set_error(user_message(message.error))
