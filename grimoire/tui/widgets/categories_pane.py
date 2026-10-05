"""The Categories pane: every category, how many documents carry it, and a way to add one.

Categories are the labels a document is tagged with.  The API returns all of
them at once (there are few), so there is no paging.  ``n`` opens a modal to
create one; tagging a document with it happens from that document's detail view.

As in the Documents pane, the client is synchronous and a thread cannot be
killed, so every request carries an id and a response for an older request is
dropped.  Names and descriptions come from the database and may contain
anything, so every cell is a Rich ``Text``, never a string parsed as markup.
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

from grimoire.api.schemas import CategoryListResponse, CategoryResponse
from grimoire.client.client import GrimoireClient
from grimoire.tui.errors import reachability, user_message
from grimoire.tui.formatting import truncate
from grimoire.tui.messages import ConnectionReport
from grimoire.tui.screens.new_category import NewCategoryScreen

_LOADING = "Loading…"
_NO_ROWS = "No categories yet. Press n to create one."
_NAME_MAX = 32
_PARENT_MAX = 24
_DOCS_WIDTH = 6
_GAP = 1  # DataTable pads each cell by one on each side; widths here are content widths
_MIN_DESCRIPTION = 12


def _widths(
    available: int, categories: list[CategoryResponse], parent_names: dict[str, str]
) -> tuple[int, int, int, int]:
    """Content widths for (name, docs, parent, description).

    Name and parent fit their longest value up to a cap; the description takes
    whatever is left and is dropped (width 0) when it would be too narrow to read.
    """
    name = min(_NAME_MAX, max([4] + [len(c.name) for c in categories]))
    parent = min(
        _PARENT_MAX,
        max([6] + [len(parent_names.get(c.parent_id or "", "")) for c in categories]),
    )
    # Each of the four columns costs two characters of cell padding.
    used = name + _DOCS_WIDTH + parent + 4 * (2 * _GAP)
    description = max(0, available - used)
    if description < _MIN_DESCRIPTION:
        description = 0
    return name, _DOCS_WIDTH, parent, description


class CategoriesPane(Vertical):
    """List categories and create new ones.

    Args:
        client: API client.  Owned by the caller.
        id: Widget id.

    Attributes:
        status_text: Plain text of the status line (loading or an error).
        footer_text: Plain text of the line under the table.
    """

    BINDINGS = [Binding("n", "new_category", "New category")]

    class Loaded(Message):
        """The categories arrived."""

        def __init__(self, request_id: int, result: CategoryListResponse) -> None:
            super().__init__()
            self.request_id = request_id
            self.result = result

    class Failed(Message):
        """The request raised."""

        def __init__(self, request_id: int, error: BaseException) -> None:
            super().__init__()
            self.request_id = request_id
            self.error = error

    def __init__(self, client: GrimoireClient, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._client = client
        self._request_id = 0
        self._has_data = False
        self._in_flight = False
        self._categories: list[CategoryResponse] = []
        self.status_text = ""
        self.footer_text = ""

    def set_client(self, client: GrimoireClient) -> None:
        """Use a different client from now on (the API key was replaced)."""
        self._client = client

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="cat-toolbar"):
            yield Button("New category", id="cat-new", variant="primary")
            yield Button("Refresh", id="cat-refresh")
        yield Static("", id="cat-status", markup=False)
        yield DataTable(id="categories-table", cursor_type="row", zebra_stripes=True)
        yield Static(_NO_ROWS, id="categories-empty", markup=False)
        yield Static("", id="cat-footer", markup=False)

    def on_mount(self) -> None:
        self.query_one("#categories-empty").display = False

    def on_resize(self, event: events.Resize) -> None:
        # The table's own width settles after the pane's.
        self.call_after_refresh(self._relayout)

    # -- hooks called by the app ---------------------------------------------

    def tab_shown(self) -> None:
        """First time the tab is shown, load.  A failed first load retries here."""
        if not self._has_data and not self._in_flight:
            self._load()

    def focus_primary(self) -> None:
        table = self.query_one("#categories-table", DataTable)
        if table.display:
            table.focus()
        else:
            self.query_one("#cat-new", Button).focus()

    def refresh_data(self) -> None:
        self._load()

    # -- actions --------------------------------------------------------------

    def action_new_category(self) -> None:
        self.app.push_screen(
            NewCategoryScreen(self._client, list(self._categories)), self._on_created
        )

    def _on_created(self, created: CategoryResponse | None) -> None:
        if created is None:
            return
        # markup=False: the name came from the user, but is shown as plain text.
        self.app.notify(f'Created category "{created.name}".', markup=False)
        self._load()

    @on(Button.Pressed, "#cat-new")
    def _on_new_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_new_category()

    @on(Button.Pressed, "#cat-refresh")
    def _on_refresh_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.refresh_data()

    # -- loading --------------------------------------------------------------

    def _load(self) -> None:
        self._request_id += 1
        self._in_flight = True
        self._set_status(_LOADING)
        self._fetch(self._request_id)

    @work(thread=True, exclusive=True, group="categories", exit_on_error=False)
    def _fetch(self, request_id: int) -> None:
        worker = get_current_worker()
        message: Message
        try:
            message = self.Loaded(request_id, self._client.list_categories())
        except Exception as exc:
            message = self.Failed(request_id, exc)
        if not worker.is_cancelled:
            self.post_message(message)

    def on_categories_pane_loaded(self, message: Loaded) -> None:
        if message.request_id != self._request_id:
            return  # a newer request is on its way
        self._in_flight = False
        self._has_data = True
        self._categories = list(message.result.categories)
        self._set_status("")
        self._show_rows()
        self.post_message(ConnectionReport(reachable=True))

    def on_categories_pane_failed(self, message: Failed) -> None:
        if message.request_id != self._request_id:
            return
        self._in_flight = False
        text = user_message(message.error)
        self._set_status(text, error=True)
        self.app.notify(text, severity="error", markup=False)
        reachable = reachability(message.error)
        if reachable is not None:
            self.post_message(ConnectionReport(reachable=reachable))

    # -- rendering ------------------------------------------------------------

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = text
        line = self.query_one("#cat-status", Static)
        line.update(text)
        line.set_class(error, "-error")

    def _show_rows(self) -> None:
        table = self.query_one("#categories-table", DataTable)
        self._fill_table(table)
        has_rows = bool(self._categories)
        had_focus = table.has_focus
        table.display = has_rows
        self.query_one("#categories-empty").display = not has_rows
        if not has_rows and had_focus:
            # Hiding the focused table drops focus; keep the keyboard somewhere.
            self.query_one("#cat-new", Button).focus()
        count = len(self._categories)
        self.footer_text = (
            _NO_ROWS if not count else f"{count} categor{'y' if count == 1 else 'ies'}"
        )
        self.query_one("#cat-footer", Static).update(self.footer_text)

    def _fill_table(self, table: DataTable[Any]) -> None:
        parent_names = {c.id: c.name for c in self._categories}
        available = max(0, table.size.width - table.styles.scrollbar_size_vertical)
        name_w, docs_w, parent_w, desc_w = _widths(
            available, self._categories, parent_names
        )
        previous = table.cursor_row
        table.clear(columns=True)
        # Explicit widths, so the table does not measure its own cells (a frame
        # drawn before DataTable's lazy measurement can otherwise stay clipped).
        table.add_column("Name", width=name_w)
        table.add_column("Docs", width=docs_w)
        table.add_column("Parent", width=parent_w)
        if desc_w:
            table.add_column("Description", width=desc_w)
        for cat in self._categories:
            cells = [
                Text(truncate(" ".join(cat.name.split()), name_w)),
                Text(str(cat.document_count), justify="right"),
                Text(truncate(parent_names.get(cat.parent_id or "", "-"), parent_w)),
            ]
            if desc_w:
                cells.append(Text(truncate(" ".join(cat.description.split()), desc_w)))
            table.add_row(*cells)
        if self._categories:
            table.move_cursor(row=min(previous, len(self._categories) - 1))

    def _relayout(self) -> None:
        if self._categories:
            self._fill_table(self.query_one("#categories-table", DataTable))
