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
        old_client.close()
        self.connection_bar.set_state(self.config.is_configured, self.config.base_url)
        self.statusBar().showMessage("API key updated for this session", 5000)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        """Drain workers and stop child processes before the window closes."""
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            shutdown = getattr(tab, "shutdown", None)
            if callable(shutdown):
                shutdown()
        if not self.pool.waitForDone(_SHUTDOWN_GRACE_MS):
            logger.warning("GUI worker pool did not drain before shutdown")
        self.client.close()
        super().closeEvent(event)
