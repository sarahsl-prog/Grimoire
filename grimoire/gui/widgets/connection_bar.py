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

        Args:
            configured: Whether an API key is present.
            base_url: The API root currently in use.
        """
        if configured:
            self._status.setText(f"Connected to {base_url}")
            self._key_field.setVisible(False)
            self._apply.setVisible(False)
        else:
            self._status.setText(
                f"No API key. Set GRIMOIRE_API_KEY (API at {base_url}) "
                "or paste one here."
            )
            self._key_field.setVisible(True)
            self._apply.setVisible(True)

    def status_text(self) -> str:
        """Current status line. Used by tests and by the status bar."""
        return self._status.text()
