"""A modal showing one document in full.

The Documents table shows a summary row; this shows the rest, including the full
server-side source path (which the table deliberately leaves out) and, for a
document that failed to process, why.

Every value comes from the database or from ingested files, so it is untrusted:
each value widget is created with ``markup=False``, and a very long value (a
stack trace in an error message, say) is capped rather than laid out in full.

Tags can be edited here: ``a`` adds one (from the categories the document does
not have), ``x`` removes one.  Both go through the API as the key in use, so a
read-tier key is refused by the server and the reason is shown in the modal.

The fetch runs in a thread worker.  A thread cannot be killed, so a screen that
is dismissed mid-fetch still has its request finish; Textual cancels the
screen's workers when it is removed, and a message posted to a screen that has
already gone is simply discarded, so the late result never lands anywhere.
"""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from textual.worker import get_current_worker

from grimoire.api.schemas import CategoryResponse, DocumentDetailResponse
from grimoire.client.client import GrimoireClient
from grimoire.tui.errors import reachability, user_message
from grimoire.tui.formatting import format_size, format_timestamp
from grimoire.tui.messages import ConnectionReport
from grimoire.tui.screens.generate import GenerateScreen
from grimoire.tui.screens.tag_picker import TagPickerScreen

_LOADING = "Loading…"
_HINT = "a add tag · x remove tag · g generate · Esc close"
_UNTITLED = "(untitled)"
_BLANK = "-"
_MAX_VALUE = 5_000
_ELLIPSIS = "…"

# (key, label) in display order.  The key is also the suffix of the widget id.
_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "ID"),
    ("title", "Title"),
    ("source_path", "Source path"),
    ("file_type", "Type"),
    ("storage_backend", "Storage"),
    ("processing_status", "Status"),
    ("size", "Size"),
    ("created", "Created"),
    ("updated", "Updated"),
    ("chunks", "Chunks"),
    ("tags", "Tags"),
    ("error", "Error"),
)


def _cap(text: str) -> str:
    """Limit a value's length so one enormous field cannot swamp the modal."""
    if len(text) <= _MAX_VALUE:
        return text
    return text[: _MAX_VALUE - 1] + _ELLIPSIS


def _shown(text: str | None, *, blank: str = _BLANK) -> str:
    """A value ready to display: stripped, capped, or ``blank`` when empty."""
    cleaned = (text or "").strip()
    return _cap(cleaned) if cleaned else blank


def _tags_text(tags: list[str]) -> str:
    """Tag names joined for one line, skipping blanks; ``-`` when there are none."""
    return _shown(", ".join(t.strip() for t in tags if t and t.strip()))


class DocumentDetailScreen(ModalScreen[bool]):
    """Show one document's details.

    ``Escape``, ``q`` or the Close button dismisses it, with ``True`` if a tag
    was added or removed (so the caller can refresh its table) and ``False``
    otherwise.  A failed fetch is shown inside the modal and does not close it,
    so the message can be read.

    Args:
        client: API client.  Owned by the caller.
        document_id: The document to fetch.

    Attributes:
        status_text: Plain text of the status line (loading or an error).
        values: The plain text currently shown for each field, by key.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("a", "add_tag", "Add tag"),
        Binding("x", "remove_tag", "Remove tag"),
        Binding("g", "generate", "Generate"),
    ]

    class Loaded(Message):
        """The document arrived."""

        def __init__(self, result: DocumentDetailResponse) -> None:
            super().__init__()
            self.result = result

    class Failed(Message):
        """The request raised."""

        def __init__(self, error: BaseException) -> None:
            super().__init__()
            self.error = error

    class TagChanged(Message):
        """A tag was added or removed on the server."""

    class TagFailed(Message):
        """An add or remove raised."""

        def __init__(self, error: BaseException) -> None:
            super().__init__()
            self.error = error

    def __init__(self, client: GrimoireClient, document_id: str) -> None:
        super().__init__()
        self._client = client
        self._document_id = document_id
        self._categories: list[CategoryResponse] = []  # the document's own tags
        self._title = ""  # for the Generate heading, once loaded
        self._changed = False
        self.status_text = _LOADING
        self.values: dict[str, str] = {key: _BLANK for key, _ in _FIELDS}

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-box"):
            yield Static("Document", id="detail-heading", markup=False)
            yield Static(_LOADING, id="detail-status", markup=False)
            with VerticalScroll(id="detail-scroll"), Grid(id="detail-grid"):
                for key, label in _FIELDS:
                    yield Static(
                        label,
                        id=f"detail-{key}-key",
                        classes="detail-key",
                        markup=False,
                    )
                    yield Static(
                        _BLANK,
                        id=f"detail-{key}",
                        classes="detail-value",
                        markup=False,
                    )
            # The app footer under a modal still shows the pane behind it, so the
            # keys that only exist in here have to be named in here.
            yield Static(_HINT, id="detail-hint", markup=False)
            yield Button("Close", id="detail-close")

    def on_mount(self) -> None:
        # Hidden until a document actually carries an error message.
        self._show_error_row(False)
        self.query_one("#detail-close", Button).focus()
        self._fetch()

    # -- fetching -----------------------------------------------------------

    @work(thread=True, exclusive=True, group="detail", exit_on_error=False)
    def _fetch(self) -> None:
        """Call the API off the event loop and post the outcome."""
        worker = get_current_worker()
        message: Message
        try:
            message = self.Loaded(self._client.get_document(self._document_id))
        except Exception as exc:
            message = self.Failed(exc)
        if not worker.is_cancelled:
            self.post_message(message)

    def on_document_detail_screen_loaded(self, message: Loaded) -> None:
        result = message.result
        self._categories = list(result.categories)
        self._title = (result.title or "").strip() or result.source_path
        self._set_status("")
        self._set_values(
            {
                "id": _shown(result.id),
                "title": _shown(result.title, blank=_UNTITLED),
                "source_path": _shown(result.source_path),
                "file_type": _shown(result.file_type),
                "storage_backend": _shown(result.storage_backend),
                "processing_status": _shown(result.processing_status),
                "size": format_size(result.size_bytes),
                "created": format_timestamp(result.created_at),
                "updated": format_timestamp(result.updated_at),
                "chunks": str(result.chunk_count),
                "tags": _tags_text(result.tags),
                "error": _shown(result.error_message),
            }
        )
        self._show_error_row(bool((result.error_message or "").strip()))
        self.post_message(ConnectionReport(reachable=True))

    def on_document_detail_screen_failed(self, message: Failed) -> None:
        # Shown inline only: the modal covers the screen, and a toast on top of
        # the very text it repeats would be noise.
        self._set_status(user_message(message.error), error=True)
        reachable = reachability(message.error)
        if reachable is not None:
            self.post_message(ConnectionReport(reachable=reachable))

    # -- rendering ----------------------------------------------------------

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = text
        line = self.query_one("#detail-status", Static)
        line.update(text)
        line.set_class(error, "-error")
        line.display = bool(text)

    def _set_values(self, values: dict[str, str]) -> None:
        self.values.update(values)
        for key, text in values.items():
            self.query_one(f"#detail-{key}", Static).update(text)

    def _show_error_row(self, visible: bool) -> None:
        self.query_one("#detail-error-key").display = visible
        self.query_one("#detail-error").display = visible

    # -- actions ------------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(self._changed)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "detail-close":
            event.stop()
            self.dismiss(self._changed)

    # -- generating ---------------------------------------------------------

    def action_generate(self) -> None:
        self.app.push_screen(
            GenerateScreen(self._client, self._document_id, self._title or _UNTITLED)
        )

    # -- tag editing --------------------------------------------------------

    def action_add_tag(self) -> None:
        self.app.push_screen(
            TagPickerScreen(
                "Add a tag",
                client=self._client,
                exclude=frozenset(c.id for c in self._categories),
                empty_text="This document already has every category.",
            ),
            lambda chosen: self._edit_tag(chosen, remove=False),
        )

    def action_remove_tag(self) -> None:
        if not self._categories:
            self._set_status("This document has no tags to remove.")
            return
        self.app.push_screen(
            TagPickerScreen("Remove a tag", categories=list(self._categories)),
            lambda chosen: self._edit_tag(chosen, remove=True),
        )

    def _edit_tag(self, chosen: CategoryResponse | None, *, remove: bool) -> None:
        if chosen is None:
            return
        self._set_status("Removing tag…" if remove else "Adding tag…")
        self._apply_tag(chosen.id, remove)

    @work(thread=True, exit_on_error=False, group="tag")
    def _apply_tag(self, category_id: str, remove: bool) -> None:
        worker = get_current_worker()
        message: Message
        try:
            if remove:
                self._client.untag_document(self._document_id, category_id)
            else:
                self._client.tag_document(self._document_id, category_id)
            message = self.TagChanged()
        except Exception as exc:
            message = self.TagFailed(exc)
        if not worker.is_cancelled:
            self.post_message(message)

    @on(TagChanged)
    def _on_tag_changed(self, message: TagChanged) -> None:
        message.stop()
        self._changed = True
        self._set_status("")
        self._fetch()  # shows the new tag list

    @on(TagFailed)
    def _on_tag_failed(self, message: TagFailed) -> None:
        message.stop()
        self._set_status(user_message(message.error), error=True)
        reachable = reachability(message.error)
        if reachable is not None:
            self.post_message(ConnectionReport(reachable=reachable))
