"""One-line connection status: API address, key state, reachability."""

from __future__ import annotations

from typing import Literal

from textual.widgets import Static

from grimoire.tui.formatting import display_url

ConnectionState = Literal["checking", "connected", "unreachable"]

# Words as well as symbols, so the state never depends on colour alone.
_INDICATORS: dict[ConnectionState, str] = {
    "checking": "… checking",
    "connected": "● connected",
    "unreachable": "○ unreachable",
}


class StatusBar(Static):
    """Shows where the TUI is pointed and whether that place answers.

    Displays the API address without credentials or path, and whether a key is
    set.  It never displays the key itself.  Text is plain (``markup=False``),
    because the address comes from the environment and must not be parsed as
    markup.

    Attributes:
        state: Current connection state.
        text: The plain text currently shown, exposed for tests and for
            anything that wants to read it without querying the renderer.
    """

    def __init__(self, base_url: str, has_key: bool) -> None:
        self._target = display_url(base_url)
        self._key_label = "key: set" if has_key else "key: missing"
        self.state: ConnectionState = "checking"
        self.text = self._compose_text()
        super().__init__(self.text, markup=False, id="status-bar")
        self.add_class("-checking")

    def _compose_text(self) -> str:
        return f"{self._target} · {self._key_label} · {_INDICATORS[self.state]}"

    def set_state(self, state: ConnectionState) -> None:
        """Change the connection indicator and its colour class."""
        self.state = state
        for name in _INDICATORS:
            self.set_class(name == state, f"-{name}")
        self.text = self._compose_text()
        self.update(self.text)
