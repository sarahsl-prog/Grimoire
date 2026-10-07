"""The Grimoire terminal application: tabs, status bar, health check, keys.

This module is the shell.  The Search/Ask and Documents panes are built by two
factory methods so they can be developed, and replaced in tests, independently.

Threading model: the API client is synchronous, so every call runs in a Textual
thread worker.  Workers never touch widgets; they post a message, which Textual
delivers on the event loop.  A thread worker cannot be killed, so an
``exclusive`` worker that has been superseded still runs to completion, and
therefore checks ``is_cancelled`` before reporting.

Ownership: whoever built the client closes it (``grimoire.tui.__main__`` does).
The app only uses it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from loguru import logger
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Footer, Header, TabbedContent, TabPane
from textual.worker import get_current_worker

from grimoire.client.client import GrimoireClient
from grimoire.client.config import ClientConfig
from grimoire.client.errors import ConnectionFailed
from grimoire.tui.messages import ConnectionReport
from grimoire.tui.screens.api_key import ApiKeyScreen
from grimoire.tui.widgets.categories_pane import CategoriesPane
from grimoire.tui.widgets.documents_pane import DocumentsPane
from grimoire.tui.widgets.ingest_pane import IngestPane
from grimoire.tui.widgets.search_pane import SearchPane
from grimoire.tui.widgets.status_bar import StatusBar
from grimoire.tui.widgets.watch_pane import WatchPane

_NO_KEY_WARNING = (
    "No API key set. Press Ctrl+K to enter one, or export GRIMOIRE_API_KEY "
    "and relaunch."
)


class GrimoireApp(App[None]):
    """Terminal client for a running Grimoire API server.

    Args:
        client: API client.  Owned by the caller, who closes it.
        config: The configuration the client was built from.
        log_path: Where this session's log file is, or None if logging is off.
        client_factory: Builds a client from a config.  Used when a key is
            entered in the app, which needs a new client (the key is a default
            header, fixed when a client is built).  The app closes the clients
            it builds, never the one it was given.
    """

    # Absolute: Textual resolves a relative CSS_PATH against the file that
    # defines the *subclass*, so a relative path silently breaks for any
    # subclass living outside this package (tests, plugins).
    CSS_PATH = str(Path(__file__).with_name("grimoire.tcss"))
    TITLE = "Grimoire"

    # `?` is typed into an Input when one has focus; use Ctrl+P (the command
    # palette) to reach the help panel from there.
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("f1", "show_tab('search')", "Search"),
        Binding("f2", "show_tab('documents')", "Documents"),
        Binding("f3", "show_tab('categories')", "Categories"),
        Binding("f4", "show_tab('ingest')", "Ingest"),
        Binding("f5", "show_tab('watch')", "Watch"),
        Binding("ctrl+r", "refresh_all", "Refresh"),
        # priority: a focused Input binds Ctrl+K to "delete to end of line", and
        # the query box has the keyboard most of the time.
        Binding("ctrl+k", "enter_api_key", "API key", priority=True),
        Binding("question_mark", "show_help_panel", "Help"),
    ]

    def __init__(
        self,
        client: GrimoireClient,
        config: ClientConfig,
        log_path: Path | None,
        client_factory: Callable[[ClientConfig], GrimoireClient] = GrimoireClient,
    ) -> None:
        super().__init__()
        self._client = client
        self._config = config
        self._log_path = log_path
        self._client_factory = client_factory
        self._last_shown_pane: TabPane | None = None
        # Clients this app built (never the caller's).  An earlier one is kept,
        # not closed, when a key is replaced: a worker already running holds it,
        # and closing it would fail that call in an unrelated pane.  All are
        # closed on exit.
        self._owned_clients: list[GrimoireClient] = []

    # -- pane factories ---------------------------------------------------

    def make_search_pane(self) -> Widget:
        """Build the Search/Ask pane."""
        return SearchPane(self._client, id="search-pane")

    def make_documents_pane(self) -> Widget:
        """Build the Documents pane."""
        return DocumentsPane(self._client, id="documents-pane")

    def make_categories_pane(self) -> Widget:
        """Build the Categories pane."""
        return CategoriesPane(self._client, id="categories-pane")

    def make_ingest_pane(self) -> Widget:
        """Build the Ingest pane."""
        return IngestPane(self._client, id="ingest-pane")

    def make_watch_pane(self) -> Widget:
        """Build the Watch pane."""
        return WatchPane(self._client, id="watch-pane")

    # -- composition and lifecycle ----------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBar(self._config.base_url, self._config.is_configured)
        with TabbedContent(initial="search"):
            yield TabPane("Search / Ask", self.make_search_pane(), id="search")
            yield TabPane("Documents", self.make_documents_pane(), id="documents")
            yield TabPane("Categories", self.make_categories_pane(), id="categories")
            yield TabPane("Ingest", self.make_ingest_pane(), id="ingest")
            yield TabPane("Watch", self.make_watch_pane(), id="watch")
        yield Footer()

    def on_mount(self) -> None:
        if not self._config.is_configured:
            # Once, at launch.  `refresh_all` never repeats it.
            self.notify(_NO_KEY_WARNING, severity="warning", timeout=10)
        self._check_health()

    def on_unmount(self) -> None:
        for client in self._owned_clients:
            try:
                client.close()
            except Exception:
                logger.exception("Closing a client raised")
        self._owned_clients.clear()

    # -- API key entry ----------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # No second key modal on top of the first (the binding is a priority
        # one, so it would otherwise fire inside the modal itself).
        return not (action == "enter_api_key" and isinstance(self.screen, ApiKeyScreen))

    def action_enter_api_key(self) -> None:
        self.push_screen(ApiKeyScreen(), self._apply_api_key)

    def _apply_api_key(self, key: str | None) -> None:
        """Rebuild the client around a key entered in the app.

        Mirrors the desktop GUI: the old client and config are kept if the new
        client cannot be built, so a failure never leaves the app without one.
        The key is never logged and never shown, including here.
        """
        if not key:
            return
        new_config = self._config.with_api_key(key)
        try:
            new_client = self._client_factory(new_config)
        except ConnectionFailed as exc:
            self.notify(exc.message, severity="error", markup=False)
            return
        self._owned_clients.append(new_client)
        self._client = new_client
        self._config = new_config
        for pane in self.query(TabPane):
            for child in pane.children:
                setter = getattr(child, "set_client", None)
                if callable(setter):
                    setter(new_client)
        bar = self.query_one(StatusBar)
        bar.set_key(new_config.is_configured)
        self.notify("API key updated for this session.", markup=False)
        logger.info("API key replaced for this session")
        self.action_refresh_all()

    # -- health check ------------------------------------------------------

    @work(thread=True, exclusive=True, group="health", exit_on_error=False)
    def _check_health(self) -> None:
        """Ask the API whether it is up, off the event loop.

        ``exclusive`` supersedes an earlier check, but cannot stop its thread,
        so a superseded check still returns; its answer is dropped here.
        """
        worker = get_current_worker()
        try:
            reachable = self._client.health()
        except Exception:
            # health() promises not to raise.  If it does, that is a bug to be
            # logged, not a reason to take the whole UI down.
            logger.exception("Health check raised unexpectedly")
            reachable = False
        if not worker.is_cancelled:
            self.post_message(ConnectionReport(reachable))

    def on_ingest_pane_completed(self, message: IngestPane.Completed) -> None:
        """A file was ingested: the document and category views are now stale."""
        for documents in self.query(DocumentsPane):
            documents.mark_stale()
        for categories in self.query(CategoriesPane):
            categories.mark_stale()

    def on_connection_report(self, message: ConnectionReport) -> None:
        try:
            bar = self.query_one(StatusBar)
        except NoMatches:
            return  # a late report arriving while the app shuts down
        bar.set_state("connected" if message.reachable else "unreachable")

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Tell the shown pane it is on screen, once layout has caught up.

        Deferred because the pane is not displayed until the tab switch has been
        laid out, and a hidden widget cannot take focus.
        """
        self.call_after_refresh(self._pane_shown, event.pane)

    def _pane_shown(self, pane: TabPane) -> None:
        """Run a pane's opt-in hooks: ``tab_shown()`` then ``focus_primary()``.

        ``tab_shown()`` is for first-time work such as loading data;
        ``focus_primary()`` takes the keyboard.

        Both are skipped unless ``pane`` is *still* the active tab.  This is a
        correctness guard, not a nicety: focusing a widget inside a pane makes
        ``TabbedContent`` activate that pane's tab.  If a callback queued for a
        tab the user has since left were allowed to run, its ``focus_primary()``
        would pull focus (and so the tab) back, which queues the other pane's
        callback, which does the same in return: an endless ping-pong.
        """
        if self.query_one(TabbedContent).active_pane is not pane:
            return
        # Textual can report one switch more than once (it briefly activates the
        # tab the focus just left, then the right one again), so the same pane
        # arrives twice in a row.  Hooks like `tab_shown()` do real work, such
        # as retrying a failed load, and must run once per switch.
        if pane is self._last_shown_pane:
            return
        self._last_shown_pane = pane
        for child in pane.children:
            for hook in ("tab_shown", "focus_primary"):
                callback = getattr(child, hook, None)
                if callable(callback):
                    callback()

    # -- actions -----------------------------------------------------------

    def action_show_tab(self, tab_id: str) -> None:
        tabs = self.query_one(TabbedContent)
        if tabs.active == tab_id:
            return
        # Drop focus first.  When the pane being left is hidden, Textual
        # refocuses a widget in it, and that late focus event makes
        # TabbedContent activate the pane we are leaving: the switch silently
        # undoes itself (about one try in six when pressing F2 from the query
        # box).  With nothing focused there is nothing to refocus; the shown
        # pane's `focus_primary()` then takes the keyboard.
        self.set_focus(None)
        tabs.active = tab_id

    def action_refresh_all(self) -> None:
        """Re-check the API and ask the visible pane to reload.

        A pane opts in by defining a ``refresh_data()`` method.
        """
        self.query_one(StatusBar).set_state("checking")
        self._check_health()
        pane = self.query_one(TabbedContent).active_pane
        if pane is None:
            return
        for child in pane.children:
            refresh = getattr(child, "refresh_data", None)
            if callable(refresh):
                refresh()
