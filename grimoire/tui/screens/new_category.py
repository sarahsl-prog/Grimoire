"""A modal for creating a category.

Name (required), description and an optional parent.  The create call runs in a
thread worker; while it runs the buttons are disabled so one ``Enter`` cannot
create the category twice.  A failure (a read-tier key, a parent that has since
gone) is shown inside the modal, which stays open so the text can be corrected
or copied; success dismisses it with the new category.

Everything shown that came from the server (parent names, error text) is
displayed with markup off.
"""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static
from textual.worker import get_current_worker

from grimoire.api.schemas import CategoryResponse
from grimoire.client.client import GrimoireClient
from grimoire.tui.errors import user_message

# The server's limit on a name (the client also checks it).
MAX_NAME = 100
_NAME_REQUIRED = "Enter a name for the category."


class NewCategoryScreen(ModalScreen[CategoryResponse | None]):
    """Ask for a new category's details; dismiss with it, or ``None`` if cancelled.

    Args:
        client: API client.  Owned by the caller.
        categories: Existing categories, offered as the parent.

    Attributes:
        error_text: Plain text of the inline error line ("" when none).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    class Created(Message):
        """The server created it."""

        def __init__(self, category: CategoryResponse) -> None:
            super().__init__()
            self.category = category

    class Failed(Message):
        """The create call raised."""

        def __init__(self, error: BaseException) -> None:
            super().__init__()
            self.error = error

    def __init__(
        self, client: GrimoireClient, categories: list[CategoryResponse]
    ) -> None:
        super().__init__()
        self._client = client
        self._categories = categories
        self._busy = False
        self.error_text = ""

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="newcat-box"):
            yield Static("New category", id="newcat-heading", markup=False)
            yield Input(
                placeholder="Name",
                max_length=MAX_NAME,
                id="newcat-name",
            )
            yield Input(placeholder="Description (optional)", id="newcat-description")
            yield Select(
                [(cat.name, cat.slug) for cat in self._categories],
                prompt="No parent",
                id="newcat-parent",
            )
            yield Static("", id="newcat-error", markup=False)
            with Horizontal(id="newcat-buttons"):
                yield Button("Create", id="newcat-create", variant="primary")
                yield Button("Cancel", id="newcat-cancel")

    def on_mount(self) -> None:
        self.query_one("#newcat-error").display = False
        self.query_one("#newcat-name", Input).focus()

    # -- state ----------------------------------------------------------------

    def _set_error(self, text: str) -> None:
        self.error_text = text
        line = self.query_one("#newcat-error", Static)
        line.update(text)
        line.display = bool(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button_id in ("#newcat-create", "#newcat-cancel"):
            self.query_one(button_id, Button).disabled = busy

    # -- actions --------------------------------------------------------------

    def action_cancel(self) -> None:
        # Not while the create call is out: dismissing now would drop its result
        # and leave the person unsure whether the category exists.
        if not self._busy:
            self.dismiss(None)

    @on(Button.Pressed, "#newcat-cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    @on(Button.Pressed, "#newcat-create")
    def _on_create_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._submit()

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def _submit(self) -> None:
        if self._busy:
            return
        name = self.query_one("#newcat-name", Input).value.strip()
        if not name:
            self._set_error(_NAME_REQUIRED)
            return
        description = self.query_one("#newcat-description", Input).value.strip()
        parent = self.query_one("#newcat-parent", Select)
        parent_slug = None if parent.is_blank() else str(parent.value)
        self._set_error("")
        self._set_busy(True)
        self._create(name, description, parent_slug)

    @work(thread=True, exit_on_error=False)
    def _create(self, name: str, description: str, parent_slug: str | None) -> None:
        """Call the API off the event loop and post the outcome."""
        worker = get_current_worker()
        message: Message
        try:
            message = self.Created(
                self._client.create_category(
                    name, description=description, parent_slug=parent_slug
                )
            )
        except Exception as exc:
            message = self.Failed(exc)
        if not worker.is_cancelled:
            self.post_message(message)

    def on_new_category_screen_created(self, message: Created) -> None:
        self._set_busy(False)
        self.dismiss(message.category)

    def on_new_category_screen_failed(self, message: Failed) -> None:
        self._set_busy(False)
        self._set_error(user_message(message.error))
