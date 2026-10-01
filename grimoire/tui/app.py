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

from pathlib import Path

from loguru import logger
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane
from textual.worker import get_current_worker

from grimoire.gui.client import GrimoireClient
from grimoire.gui.config import GuiConfig
from grimoire.tui.messages import ConnectionReport
from grimoire.tui.widgets.search_pane import SearchPane
from grimoire.tui.widgets.status_bar import StatusBar

_NO_KEY_WARNING = "No API key set. Export GRIMOIRE_API_KEY and relaunch."


class GrimoireApp(App[None]):
    """Terminal client for a running Grimoire API server.

    Args:
        client: API client.  Owned by the caller, who closes it.
        config: The configuration the client was built from.
        log_path: Where this session's log file is, or None if logging is off.
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
        Binding("ctrl+r", "refresh_all", "Refresh"),
        Binding("question_mark", "show_help_panel", "Help"),
    ]

    def __init__(
        self, client: GrimoireClient, config: GuiConfig, log_path: Path | None
    ) -> None:
        super().__init__()
        self._client = client
        self._config = config
        self._log_path = log_path

    # -- pane factories ---------------------------------------------------

    def make_search_pane(self) -> Widget:
        """Build the Search/Ask pane."""
        return SearchPane(self._client, id="search-pane")

    def make_documents_pane(self) -> Widget:
        """Build the Documents pane.  Replaced by the real pane in a later task."""
        return Static("Documents are coming.", id="documents-pane")

    # -- composition and lifecycle ----------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBar(self._config.base_url, self._config.is_configured)
        with TabbedContent(initial="search"):
            yield TabPane("Search / Ask", self.make_search_pane(), id="search")
            yield TabPane("Documents", self.make_documents_pane(), id="documents")
        yield Footer()

    def on_mount(self) -> None:
        if not self._config.is_configured:
            # Once, at launch.  `refresh_all` never repeats it.
            self.notify(_NO_KEY_WARNING, severity="warning", timeout=10)
        self._check_health()

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

    def on_connection_report(self, message: ConnectionReport) -> None:
        try:
            bar = self.query_one(StatusBar)
        except NoMatches:
            return  # a late report arriving while the app shuts down
        bar.set_state("connected" if message.reachable else "unreachable")

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Give the shown pane the keyboard: a pane opts in with ``focus_primary()``."""
        for child in event.pane.children:
            focus = getattr(child, "focus_primary", None)
            if callable(focus):
                # After refresh: the pane is not displayed until the tab switch
                # has been laid out, and a hidden widget cannot take focus.
                self.call_after_refresh(focus)

    # -- actions -----------------------------------------------------------

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

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
