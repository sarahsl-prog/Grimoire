"""A modal for entering the API key without relaunching.

The key is held in memory only.  Nothing here writes it anywhere, and nothing
echoes it: the input is masked, validation errors never repeat what was typed,
and the dismissed value goes straight to the app, which applies it to a new
client.  The key is a credential, so it is also kept out of every message this
screen shows.
"""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

# Printable ASCII with no whitespace.  A header value cannot carry a newline or
# other control character (it would break the request, or worse, inject one),
# and a pasted key never legitimately contains a space.  `fullmatch`, not `$`,
# which also matches before a trailing newline.
_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,256}")

_EMPTY = "Enter an API key."
_INVALID = (
    "That does not look like an API key: use letters, digits and symbols only, "
    "with no spaces, up to 256 characters."
)


def parse_api_key(text: str) -> str | None:
    """Return the key in ``text`` (surrounding whitespace removed), or None."""
    cleaned = text.strip()
    return cleaned if _KEY_PATTERN.fullmatch(cleaned) else None


class ApiKeyScreen(ModalScreen[str | None]):
    """Ask for an API key; dismiss with the key, or ``None`` if cancelled.

    ``Enter`` accepts, ``Escape`` cancels.  A bad value is explained inside the
    modal, which stays open so it can be corrected.

    Attributes:
        error_text: Plain text of the inline error line ("" when none).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        self.error_text = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="apikey-box"):
            yield Static("API key", id="apikey-heading", markup=False)
            yield Static(
                "Used for this session only. It is never saved to disk.",
                id="apikey-help",
                markup=False,
            )
            yield Input(
                password=True,
                placeholder="Paste your API key",
                id="apikey-input",
            )
            yield Static("", id="apikey-error", markup=False)
            with Horizontal(id="apikey-buttons"):
                yield Button("Use key", id="apikey-save", variant="primary")
                yield Button("Cancel", id="apikey-cancel")

    def on_mount(self) -> None:
        self.query_one("#apikey-error").display = False
        self.query_one("#apikey-input", Input).focus()

    def _set_error(self, text: str) -> None:
        self.error_text = text
        line = self.query_one("#apikey-error", Static)
        line.update(text)
        line.display = bool(text)

    def _submit(self) -> None:
        field = self.query_one("#apikey-input", Input)
        raw = field.value
        if not raw.strip():
            self._set_error(_EMPTY)
            return
        key = parse_api_key(raw)
        if key is None:
            self._set_error(_INVALID)
            return
        # Clear the field before handing the key over, so it does not linger in
        # a widget that may be kept alive while the modal animates away.
        field.value = ""
        self.dismiss(key)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        if self.error_text:
            self._set_error("")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "apikey-save":
            self._submit()
        elif event.button.id == "apikey-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.query_one("#apikey-input", Input).value = ""
        self.dismiss(None)
