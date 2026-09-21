"""Entry point for the Grimoire desktop client.

Run with ``grimoire-gui`` after ``uv sync --extra gui``.
"""

from __future__ import annotations

import sys
from uuid import uuid4

from loguru import logger


def main() -> int:
    """Start the GUI.

    Returns:
        The Qt application's exit code, 1 when PySide6 is not installed, or
        1 when the configured API URL is malformed.
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "PySide6 is not installed. Run: uv sync --extra gui",
            file=sys.stderr,
        )
        return 1

    from grimoire.gui.app import MainWindow
    from grimoire.gui.client import GrimoireClient
    from grimoire.gui.config import GuiConfig
    from grimoire.gui.errors import ConnectionFailed

    session_id = uuid4().hex[:12]
    # Bound once so every record from this launch can be isolated in the log,
    # matching the project's context-in-log-records convention.
    logger.configure(extra={"session_id": session_id})
    logger.info(f"Starting Grimoire GUI (session {session_id})")

    config = GuiConfig.from_env()
    try:
        client = GrimoireClient(config)
    except ConnectionFailed as exc:
        # No window exists yet, so there is nothing to show a dialog on - a
        # typo in GRIMOIRE_API_URL must fail with a message, not a traceback.
        print(exc.message, file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName("Grimoire")
    window = MainWindow(client, config)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
