"""A modal that generates content from one document.

Pick a kind (summary, flash cards, cliff notes, outline, or an extract, which
also asks what to extract), press Generate, and read the result in place.  An
LLM does the work and can take a minute or more, so the call runs in a thread
worker with the client's long timeout; the screen says it is working, and can be
closed meanwhile (the server keeps going, and its answer is discarded).

The result is derived from ingested documents, which in the security corpus can
be hostile, so it is shown as plain text, never parsed as markup, and a very long
one is capped rather than laid out in full.
"""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static
from textual.worker import get_current_worker

from grimoire.api.schemas import GenerateResponse
from grimoire.client.client import GrimoireClient
from grimoire.tui.errors import reachability, user_message
from grimoire.tui.messages import ConnectionReport

_WORKING = "Generating… this can take a minute or more. Esc closes this; the server may keep working."
_EMPTY_RESULT = "(The model returned no text.)"
_MAX_OUTPUT = 50_000
_ELLIPSIS = "…"
_TRUNCATED = "\n\n(Output truncated for display.)"

# (label shown, value sent), in display order.
KINDS: tuple[tuple[str, str], ...] = (
    ("Summary", "summary"),
    ("Flash cards", "flash_card"),
    ("Cliff notes", "cliff_notes"),
    ("Outline", "outline"),
    ("Extract (answer a question)", "extract"),
)


def _capped(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return _EMPTY_RESULT
    if len(cleaned) <= _MAX_OUTPUT:
        return cleaned
    return cleaned[: _MAX_OUTPUT - 1] + _ELLIPSIS + _TRUNCATED


def describe(result: GenerateResponse) -> str:
    """One line about how a result was made: model, time, and whether cached."""
    parts = []
    if result.model_used:
        parts.append(result.model_used)
    if result.duration_ms:
        parts.append(f"{result.duration_ms / 1000:.1f}s")
    if result.cached:
        parts.append("cached")
    return " · ".join(parts)


class GenerateScreen(ModalScreen[None]):
    """Generate and show content for one document.

    Args:
        client: API client.  Owned by the caller.
        document_id: The document to generate from.
        title: Shown in the heading.

    Attributes:
        status_text: Plain text of the status line (working, an error, or "").
        output_text: Plain text of the result currently shown.
        meta_text: Plain text of the "model · time" line.
    """

    BINDINGS = [Binding("escape", "close", "Close")]

    class Generated(Message):
        """The server returned content."""

        def __init__(self, result: GenerateResponse) -> None:
            super().__init__()
            self.result = result

    class Failed(Message):
        """The request raised."""

        def __init__(self, error: BaseException) -> None:
            super().__init__()
            self.error = error

    def __init__(self, client: GrimoireClient, document_id: str, title: str) -> None:
        super().__init__()
        self._client = client
        self._document_id = document_id
        self._title = title
        self._busy = False
        self.status_text = ""
        self.output_text = ""
        self.meta_text = ""

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="gen-box"):
            yield Static(
                f"Generate from: {self._title}", id="gen-heading", markup=False
            )
            with Horizontal(id="gen-controls"):
                yield Select(
                    [(label, value) for label, value in KINDS],
                    allow_blank=False,
                    value="summary",
                    id="gen-kind",
                )
                yield Button("Generate", id="gen-go", variant="primary")
            yield Input(placeholder="What should be extracted?", id="gen-query")
            yield Static("", id="gen-status", markup=False)
            with VerticalScroll(id="gen-result"):
                yield Static("", id="gen-output", markup=False)
            yield Static("", id="gen-meta", markup=False)
            yield Button("Close", id="gen-close")

    def on_mount(self) -> None:
        self.query_one("#gen-query").display = False
        self.query_one("#gen-result").display = False
        self.query_one("#gen-status").display = False
        self.query_one("#gen-meta").display = False
        self.query_one("#gen-go", Button).focus()

    # -- state ----------------------------------------------------------------

    @property
    def _kind(self) -> str:
        return str(self.query_one("#gen-kind", Select).value)

    @on(Select.Changed, "#gen-kind")
    def _on_kind_changed(self, event: Select.Changed) -> None:
        event.stop()
        # The query box only means something for an extract.
        self.query_one("#gen-query").display = self._kind == "extract"

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = text
        line = self.query_one("#gen-status", Static)
        line.update(text)
        line.set_class(error, "-error")
        line.display = bool(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query_one("#gen-go", Button).disabled = busy
        self.query_one("#gen-kind", Select).disabled = busy

    # -- actions --------------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#gen-close")
    def _on_close_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    @on(Button.Pressed, "#gen-go")
    def _on_go_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._submit()

    @on(Input.Submitted, "#gen-query")
    def _on_query_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def _submit(self) -> None:
        if self._busy:
            return
        kind = self._kind
        query = (
            self.query_one("#gen-query", Input).value.strip()
            if kind == "extract"
            else None
        )
        if kind == "extract" and not query:
            self._set_status("Say what to extract.", error=True)
            return
        self._set_busy(True)
        self._set_status(_WORKING)
        self._generate(kind, query)

    @work(thread=True, exit_on_error=False)
    def _generate(self, kind: str, query: str | None) -> None:
        """Call the API off the event loop and post the outcome."""
        worker = get_current_worker()
        message: Message
        try:
            message = self.Generated(
                self._client.generate([self._document_id], kind, query=query)
            )
        except Exception as exc:
            message = self.Failed(exc)
        if not worker.is_cancelled:
            self.post_message(message)

    def on_generate_screen_generated(self, message: Generated) -> None:
        result = message.result
        self._set_busy(False)
        self._set_status("")
        self.output_text = _capped(result.content)
        self.meta_text = describe(result)
        self.query_one("#gen-output", Static).update(self.output_text)
        meta = self.query_one("#gen-meta", Static)
        meta.update(self.meta_text)
        meta.display = bool(self.meta_text)
        scroll = self.query_one("#gen-result", VerticalScroll)
        scroll.display = True
        scroll.scroll_home(animate=False)
        scroll.focus()  # so the arrow keys and Page Up/Down scroll the text
        self.post_message(ConnectionReport(reachable=True))

    def on_generate_screen_failed(self, message: Failed) -> None:
        self._set_busy(False)
        self._set_status(user_message(message.error), error=True)
        # Disabling the button took the keyboard with it; give it back so a
        # retry is one keypress away.
        self.query_one("#gen-go", Button).focus()
        reachable = reachability(message.error)
        if reachable is not None:
            self.post_message(ConnectionReport(reachable=reachable))
