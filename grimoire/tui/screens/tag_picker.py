"""A modal that picks one category from a list.

Used by the document detail view for both directions of tag editing: adding
(the categories a document does *not* have, fetched here) and removing (the
ones it does, passed in).  ``Enter`` picks, ``Escape`` cancels.

Category names come from the database and may contain anything, so each option
is a Rich ``Text`` rather than a string parsed as markup.
"""

from __future__ import annotations

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from grimoire.api.schemas import CategoryListResponse, CategoryResponse
from grimoire.client.client import GrimoireClient
from grimoire.tui.errors import user_message
from grimoire.tui.formatting import truncate

_LOADING = "Loading…"
_NAME_MAX = 60


class TagPickerScreen(ModalScreen[CategoryResponse | None]):
    """Choose a category; dismiss with it, or ``None`` if cancelled.

    Args:
        heading: Title line, e.g. "Add a tag".
        categories: The choices.  When ``None`` they are fetched with ``client``.
        client: Used only when ``categories`` is ``None``.
        exclude: Category ids to leave out of a fetched list (those the document
            already has).
        empty_text: Shown when there is nothing to choose from.

    Attributes:
        status_text: Plain text of the status line (loading, an error, or "").
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    class Loaded(Message):
        """The categories arrived."""

        def __init__(self, result: CategoryListResponse) -> None:
            super().__init__()
            self.result = result

    class Failed(Message):
        """The fetch raised."""

        def __init__(self, error: BaseException) -> None:
            super().__init__()
            self.error = error

    def __init__(
        self,
        heading: str,
        *,
        categories: list[CategoryResponse] | None = None,
        client: GrimoireClient | None = None,
        exclude: frozenset[str] = frozenset(),
        empty_text: str = "Nothing to choose from.",
    ) -> None:
        super().__init__()
        if categories is None and client is None:
            raise ValueError("TagPickerScreen needs categories or a client")
        self._heading = heading
        self._client = client
        self._exclude = exclude
        self._empty_text = empty_text
        self._choices: dict[str, CategoryResponse] = {}
        self.status_text = ""
        self._initial = categories

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static(self._heading, id="picker-heading", markup=False)
            yield Static("", id="picker-status", markup=False)
            yield OptionList(id="picker-list")

    def on_mount(self) -> None:
        if self._initial is not None:
            self._show(self._initial)
        elif (
            self._client is not None
        ):  # always true: __init__ requires one or the other
            self._set_status(_LOADING)
            self._fetch(self._client)

    # -- fetching -------------------------------------------------------------

    @work(thread=True, exit_on_error=False)
    def _fetch(self, client: GrimoireClient) -> None:
        worker = get_current_worker()
        message: Message
        try:
            message = self.Loaded(client.list_categories())
        except Exception as exc:
            message = self.Failed(exc)
        if not worker.is_cancelled:
            self.post_message(message)

    def on_tag_picker_screen_loaded(self, message: Loaded) -> None:
        self._show([c for c in message.result.categories if c.id not in self._exclude])

    def on_tag_picker_screen_failed(self, message: Failed) -> None:
        self._set_status(user_message(message.error), error=True)

    # -- rendering ------------------------------------------------------------

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = text
        line = self.query_one("#picker-status", Static)
        line.update(text)
        line.set_class(error, "-error")
        line.display = bool(text)

    def _show(self, categories: list[CategoryResponse]) -> None:
        option_list = self.query_one("#picker-list", OptionList)
        option_list.clear_options()
        self._choices = {c.id: c for c in categories}
        if not categories:
            self._set_status(self._empty_text)
            return
        self._set_status("")
        option_list.add_options(
            [
                Option(Text(truncate(" ".join(c.name.split()), _NAME_MAX)), id=c.id)
                for c in categories
            ]
        )
        option_list.highlighted = 0
        option_list.focus()

    # -- actions --------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        chosen = self._choices.get(event.option.id or "")
        if chosen is not None:
            self.dismiss(chosen)
