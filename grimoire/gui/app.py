"""Main window for the Grimoire desktop client."""

from __future__ import annotations

from typing import Any

from loguru import logger
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from grimoire.gui.config import GuiConfig
from grimoire.gui.errors import ConnectionFailed
from grimoire.gui.widgets.connection_bar import ConnectionBar

_ERROR_TIMEOUT_MS = 15_000
_SHUTDOWN_GRACE_MS = 3_000


class MainWindow(QMainWindow):
    """Hosts the connection bar, the tab area, and the shared thread pool.

    Args:
        client: The API client every tab shares.
        config: Configuration the client was built from.
    """

    def __init__(self, client: Any, config: GuiConfig) -> None:
        super().__init__()
        self.client = client
        self.config = config
        # Clients retired by a pasted API key: an ApiWorker already running
        # on a pool thread holds a closure over the OLD client, so closing
        # it immediately would fail that in-flight call.  These are only
        # closed once closeEvent confirms the pool has drained.
        self._retired_clients: list[Any] = []
        self.pool = QThreadPool()
        # The pipeline is CPU- and GPU-bound server side; more client
        # threads would only queue deeper on the API.
        self.pool.setMaxThreadCount(4)

        self.setWindowTitle("Grimoire")
        self.resize(1100, 760)

        self.connection_bar = ConnectionBar()
        self.connection_bar.set_state(config.is_configured, config.base_url)
        self.connection_bar.api_key_entered.connect(self._on_api_key_entered)

        self.tabs = QTabWidget()

        from grimoire.gui.widgets.search_tab import SearchTab

        self.search_tab = SearchTab(self.client, self.pool, self.show_error)
        self.tabs.addTab(self.search_tab, "Search / Ask")

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.connection_bar)
        layout.addWidget(self.tabs, stretch=1)
        self.setCentralWidget(container)

        self.statusBar().showMessage("Ready")

    def show_error(self, message: str) -> None:
        """Surface an error in the status bar.

        Tabs also show their own inline error; this is the cross-tab trail.
        """
        self.statusBar().showMessage(message, _ERROR_TIMEOUT_MS)

    def _on_api_key_entered(self, key: str) -> None:
        """Rebuild the client around a session key pasted by the user."""
        from grimoire.gui.client import GrimoireClient

        new_config = self.config.with_api_key(key)
        try:
            new_client = GrimoireClient(new_config)
        except ConnectionFailed as exc:
            # A malformed base_url would otherwise leave the window without
            # any client at all.  Keep the existing (working) client and
            # config so the user can retry instead of losing their session.
            self.show_error(exc.message)
            return

        self.config = new_config
        old_client = self.client
        self.client = new_client
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            setter = getattr(tab, "set_client", None)
            if callable(setter):
                setter(self.client)
        # Retire rather than close: a worker already in flight on the pool
        # holds a closure over old_client, and closing it under that call
        # would surface as a confusing generic failure in an unrelated tab.
        # closeEvent closes it once the pool is confirmed drained.
        self._retired_clients.append(old_client)
        self.connection_bar.set_state(self.config.is_configured, self.config.base_url)
        self.statusBar().showMessage("API key updated for this session", 5000)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        """Drain workers and stop child processes before the window closes."""
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            shutdown = getattr(tab, "shutdown", None)
            if callable(shutdown):
                shutdown()
        drained = self.pool.waitForDone(_SHUTDOWN_GRACE_MS)
        if drained:
            self.client.close()
            for retired in self._retired_clients:
                retired.close()
        else:
            # A pool thread may still hold a closure over one of these
            # clients.  The process is exiting either way, so leaking the
            # sockets is strictly better than closing them under a live
            # worker and corrupting its result.
            logger.warning(
                "GUI worker pool did not drain before shutdown; "
                f"leaking {1 + len(self._retired_clients)} client(s) "
                "instead of closing them under a live worker"
            )
        super().closeEvent(event)
