"""The Documents pane: a paged, filterable table of what the corpus holds.

It is deliberately read-only.  Rows are fetched a page at a time from
``GET /documents`` (newest first), filtered on the two fields the API supports
that are worth a dropdown, status and file type.

As in the Search pane, the client is synchronous and a thread worker cannot be
killed, so a response that arrives after a newer request has been issued must
be recognised and dropped.  Two independent guards do that: the worker checks
``is_cancelled`` before posting, and every request carries an id that the
handler compares.

Everything shown comes from ingested files or the database, and in the security
corpus that includes hostile text.  Every table cell is a Rich ``Text`` object,
never a string, so nothing in a title, type or status is parsed as markup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, DataTable, Select, Static
from textual.worker import get_current_worker

from grimoire.api.schemas import DocumentListResponse, DocumentResponse
from grimoire.gui.client import GrimoireClient
from grimoire.tui.errors import reachability, user_message
from grimoire.tui.formatting import (
    DOC_STATUSES,
    FILE_TYPES,
    format_size,
    format_timestamp,
    truncate,
)
from grimoire.tui.messages import ConnectionReport
from grimoire.tui.screens.document_detail import DocumentDetailScreen

# The API's default page size.  The offset sent is always derived from the page
# index times this, never taken from a response.
PAGE_SIZE = 50

# One long title sets the whole column's width.  At 60 it pushed Size and Added
# off a 100-column terminal; 40 leaves room for every column there.
_TITLE_WIDTH = 40
_CELL_WIDTH = 24  # type and status come from enums, so anything longer is noise
_UNTITLED = "(untitled)"
_LOADING = "Loading…"
_NO_ROWS = "No documents."

# Rich style names, not theme variables: a Text object cannot reference `$success`.
_STATUS_STYLES = {"completed": "green", "failed": "red"}
_DEFAULT_STATUS_STYLE = "dim"


@dataclass(frozen=True)
class _Request:
    """Everything a worker needs, captured on the event loop at request time."""

    id: int
    page: int
    status: str | None
    file_type: str | None
    keep_cursor: bool


def _title_for(doc: DocumentResponse) -> str:
    """The document's title, else just the file name from its path.

    Only the file name: the full server-side path is not for the table.  Both
    ``/`` and ``\\`` are separators, because a Windows client may have ingested
    the file, and cloud paths use ``scheme://``.
    """
    title = (doc.title or "").strip()
    if title:
        return title
    name = doc.source_path.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or _UNTITLED


def _pages_for(total: int) -> int:
    return max(1, -(-total // PAGE_SIZE))


class DocumentsPane(Vertical):
    """Browse documents a page at a time.

    Args:
        client: API client.  Owned by the caller.
        id: Widget id.

    Attributes:
        status_text: Plain text of the status line (loading or an error).
        footer_text: Plain text of the range line under the table.
    """

    BINDINGS = [
        Binding("right_square_bracket", "next_page", "Next page"),
        Binding("left_square_bracket", "prev_page", "Prev page"),
    ]

    class Loaded(Message):
        """A page arrived."""

        def __init__(self, request: _Request, result: DocumentListResponse) -> None:
            super().__init__()
            self.request = request
            self.result = result

    class Failed(Message):
        """A page request raised."""

        def __init__(self, request: _Request, error: BaseException) -> None:
            super().__init__()
            self.request = request
            self.error = error

    def __init__(self, client: GrimoireClient, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._client = client
        self._request_id = 0
        self._activated = False  # the tab has been shown at least once
        self._has_data = False  # a page has loaded successfully
        self._in_flight = False
        self._shown_page = 0  # the page the table currently holds
        self._target_page = 0  # the page most recently asked for
        self._total = 0
        self._ids: list[str | None] = []  # parallel to table rows
        self._focus_parked = False  # keyboard lent to Refresh while the table is hidden
        self.status_text = ""
        self.footer_text = ""

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="doc-filters"):
            yield Select(
                [(value, value) for value in DOC_STATUSES],
                prompt="Any status",
                id="status-filter",
            )
            yield Select(
                [(value, value) for value in FILE_TYPES],
                prompt="Any type",
                id="type-filter",
            )
            yield Button("Refresh", id="doc-refresh")
        yield Static("", id="doc-status", markup=False)
        yield DataTable(id="documents-table", cursor_type="row", zebra_stripes=True)
        yield Static("No documents match.", id="documents-empty", markup=False)
        yield Static("", id="doc-footer", markup=False)

    def on_mount(self) -> None:
        table = self.query_one("#documents-table", DataTable)
        table.add_columns("Title", "Type", "Status", "Size", "Added")
        self.query_one("#documents-empty").display = False

    # -- hooks called by the app -------------------------------------------

    def tab_shown(self) -> None:
        """First time the tab is shown, load.  A failed first load retries here."""
        self._activated = True
        if not self._has_data and not self._in_flight:
            self._load(0)

    def focus_primary(self) -> None:
        """Give the keyboard to the table (or the Refresh button when it is hidden)."""
        table = self.query_one("#documents-table", DataTable)
        if table.display:
            table.focus()
        else:
            self.query_one("#doc-refresh", Button).focus()

    def refresh_data(self) -> None:
        """Reload the current page, keeping the cursor on the same document."""
        if self._has_data:
            self._load(self._shown_page, keep_cursor=True)
        else:
            self._load(0)

    # -- state --------------------------------------------------------------

    @property
    def selected_document_id(self) -> str | None:
        """The id of the document under the cursor, or None if there is none."""
        row = self.query_one("#documents-table", DataTable).cursor_row
        if 0 <= row < len(self._ids):
            return self._ids[row]
        return None

    def _filters(self) -> tuple[str | None, str | None]:
        status = self.query_one("#status-filter", Select)
        file_type = self.query_one("#type-filter", Select)
        return (
            None if status.is_blank() else str(status.value),
            None if file_type.is_blank() else str(file_type.value),
        )

    # -- paging and filtering -------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Hide a paging key when it cannot apply, so the footer never offers it.
        if action == "next_page":
            return self._has_data and self._target_page + 1 < _pages_for(self._total)
        if action == "prev_page":
            return self._has_data and self._target_page > 0
        return super().check_action(action, parameters)

    def action_next_page(self) -> None:
        if self.check_action("next_page", ()):
            self._load(self._target_page + 1)

    def action_prev_page(self) -> None:
        if self.check_action("prev_page", ()):
            self._load(self._target_page - 1)

    @on(Select.Changed)
    def _on_filter_changed(self, event: Select.Changed) -> None:
        event.stop()
        # Selects announce their initial value as they mount; nothing has been
        # asked for yet, so there is nothing to reload.
        if self._activated:
            self._load(0)

    @on(DataTable.RowSelected, "#documents-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        """``Enter`` (or a click) on a row opens that document's detail."""
        event.stop()
        row = event.cursor_row
        document_id = self._ids[row] if 0 <= row < len(self._ids) else None
        if document_id:  # a row the server sent without an id cannot be fetched
            self.app.push_screen(DocumentDetailScreen(self._client, document_id))

    @on(Button.Pressed, "#doc-refresh")
    def _on_refresh_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.refresh_data()

    # -- loading --------------------------------------------------------------

    def _load(self, page: int, *, keep_cursor: bool = False) -> None:
        status, file_type = self._filters()
        self._request_id += 1
        request = _Request(self._request_id, page, status, file_type, keep_cursor)
        self._target_page = page
        self._in_flight = True
        self._set_status(_LOADING)
        self.refresh_bindings()
        self._fetch(request)

    @work(thread=True, exclusive=True, group="documents", exit_on_error=False)
    def _fetch(self, request: _Request) -> None:
        """Call the API off the event loop and post the outcome."""
        worker = get_current_worker()
        message: Message
        try:
            result = self._client.list_documents(
                offset=request.page * PAGE_SIZE,
                limit=PAGE_SIZE,
                status=request.status,
                file_type=request.file_type,
            )
            message = self.Loaded(request, result)
        except Exception as exc:
            message = self.Failed(request, exc)
        if not worker.is_cancelled:
            self.post_message(message)

    def _is_stale(self, request: _Request) -> bool:
        return request.id != self._request_id

    def on_documents_pane_loaded(self, message: Loaded) -> None:
        request, result = message.request, message.result
        if self._is_stale(request):
            return

        pages = _pages_for(result.total)
        if not result.documents and result.total > 0 and request.page >= pages:
            # The corpus shrank since the page count was learned (documents were
            # deleted elsewhere).  Go to the last page that exists.  This cannot
            # loop: the retry targets `pages - 1`, which is never `>= pages`.
            self._load(pages - 1)
            return

        self._in_flight = False
        self._total = result.total
        self._shown_page = self._target_page = request.page
        self._has_data = True
        self._show_rows(result.documents, keep_cursor=request.keep_cursor)
        self._set_status("")
        self.post_message(ConnectionReport(reachable=True))
        self.refresh_bindings()

    def on_documents_pane_failed(self, message: Failed) -> None:
        if self._is_stale(message.request):
            return
        self._in_flight = False
        # The table still shows `_shown_page`; the page that failed to load must
        # not stay the "current" one, or the next `]` would skip it.
        self._target_page = self._shown_page
        text = user_message(message.error)
        self._set_status(text, error=True)
        # markup=False: the text may come from the server.
        self.app.notify(text, severity="error", markup=False)
        reachable = reachability(message.error)
        if reachable is not None:
            self.post_message(ConnectionReport(reachable=reachable))
        self.refresh_bindings()

    # -- rendering ------------------------------------------------------------

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = text
        line = self.query_one("#doc-status", Static)
        line.update(text)
        line.set_class(error, "-error")

    def _show_rows(
        self, documents: list[DocumentResponse], *, keep_cursor: bool
    ) -> None:
        table = self.query_one("#documents-table", DataTable)
        previous_id = self.selected_document_id if keep_cursor else None
        previous_row = table.cursor_row if keep_cursor else 0

        table.clear()
        self._ids = []
        seen: set[str] = set()
        for doc in documents:
            # A duplicate or empty id would make add_row raise; let Textual
            # generate a key for it instead of taking the whole UI down.
            usable = bool(doc.id) and doc.id not in seen
            seen.add(doc.id)
            table.add_row(
                Text(truncate(_title_for(doc), _TITLE_WIDTH)),
                Text(truncate(doc.file_type, _CELL_WIDTH)),
                Text(
                    truncate(doc.processing_status, _CELL_WIDTH),
                    style=_STATUS_STYLES.get(
                        doc.processing_status, _DEFAULT_STATUS_STYLE
                    ),
                ),
                Text(format_size(doc.size_bytes), justify="right"),
                Text(format_timestamp(doc.created_at)),
                key=doc.id if usable else None,
            )
            self._ids.append(doc.id or None)

        has_rows = bool(documents)
        had_focus = table.has_focus
        table.display = has_rows
        self.query_one("#documents-empty").display = not has_rows
        if not has_rows and had_focus:
            # Hiding the focused table makes Textual drop focus, which would
            # leave the keyboard on nothing.  Hand it to the Refresh button.
            # Only when the table *had* focus: a reload that lands while the
            # user is typing in a filter must not take the keyboard from it.
            self.query_one("#doc-refresh", Button).focus()
            self._focus_parked = True
        elif has_rows and self._focus_parked:
            self._focus_parked = False
            if self.query_one("#doc-refresh", Button).has_focus:
                table.focus()
        if has_rows:
            self._restore_cursor(table, previous_id, previous_row)
            start = self._shown_page * PAGE_SIZE
            self.footer_text = (
                f"Rows {start + 1}-{start + len(documents)} of {self._total}"
                f" · page {self._shown_page + 1}/{_pages_for(self._total)}"
            )
        else:
            self.footer_text = _NO_ROWS
        self.query_one("#doc-footer", Static).update(self.footer_text)

    def _restore_cursor(
        self, table: DataTable[Any], previous_id: str | None, previous_row: int
    ) -> None:
        """Keep the cursor on the same document, else on the nearest row."""
        if previous_id is not None and previous_id in self._ids:
            table.move_cursor(row=self._ids.index(previous_id))
        else:
            table.move_cursor(row=min(previous_row, len(self._ids) - 1))
