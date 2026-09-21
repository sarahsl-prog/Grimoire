"""Connection state strip shown above the tabs."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)


class ConnectionBar(QWidget):
    """Shows which API the client talks to and whether it has a key.

    A key pasted here is kept in memory for the session only.  Nothing in
    this widget writes it to QSettings or to a file: a plaintext credential
    in ~/.config is a worse default than retyping one.
    """

    # Declared before __init__: Qt resolves signals as class attributes at
    # metaclass time, so one assigned later in the body never connects.
    api_key_entered = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._configured = False
        self._base_url = ""
        # None = no health check has completed yet. Distinct from True/False
        # so the status text can avoid claiming "Connected" - or "Cannot
        # reach" - until a real check has actually run.
        self._reachable: bool | None = None
        self._status = QLabel()
        self._key_field = QLineEdit()
        self._key_field.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_field.setPlaceholderText("Paste an API key for this session")
        self._apply = QPushButton("Use key")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(self._status, stretch=1)
        layout.addWidget(self._key_field)
        layout.addWidget(self._apply)

        self._apply.clicked.connect(self._emit_key)
        self._key_field.returnPressed.connect(self._emit_key)

    def _emit_key(self) -> None:
        key = self._key_field.text().strip()
        if key:
            self.api_key_entered.emit(key)
            self._key_field.clear()

    def set_state(self, configured: bool, base_url: str) -> None:
        """Update the displayed connection state.

        The key field and Use-key button are never hidden: `configured`
        means only `bool(api_key)` (see GuiConfig.is_configured), and
        nothing here has verified that key actually works. A key rejected
        by the server (stale, revoked, mistyped) needs to be replaceable
        without restarting the app, and this field is the only way to do
        that - hiding it the moment any key is present removes the sole
        recovery path exactly when it is needed.

        Args:
            configured: Whether an API key is present.
            base_url: The API root currently in use.
        """
        self._configured = configured
        self._base_url = base_url
        # A previous health check no longer speaks to this state - a key
        # change likely means a different server or a different key's
        # permissions - so don't let a stale "Connected" or "Cannot reach"
        # linger until the next check completes.
        self._reachable = None
        self._render_status()

    def set_health(self, reachable: bool) -> None:
        """Reflect the result of a GrimoireClient.health() check.

        Called once at startup. "Connected" is reserved for this - a state
        a real health check has actually confirmed - rather than being
        claimed the moment a key is merely present.
        """
        self._reachable = reachable
        self._render_status()

    def _render_status(self) -> None:
        if self._reachable is False:
            self._status.setText(f"Cannot reach {self._base_url}")
        elif self._configured:
            if self._reachable:
                self._status.setText(f"Connected to {self._base_url}")
            else:
                self._status.setText(f"Key set · {self._base_url}")
        else:
            self._status.setText(
                f"No API key. Set GRIMOIRE_API_KEY (API at {self._base_url}) "
                "or paste one here."
            )

    def status_text(self) -> str:
        """Current status line. Used by tests and by the status bar."""
        return self._status.text()
