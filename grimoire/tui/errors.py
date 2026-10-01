"""Turn exceptions into text that is safe to show on screen.

The project rule is that raw exceptions never reach the user.  The API client
already raises only ``GuiError`` subclasses, each carrying a complete sentence
written for a person.  Anything else is a bug: it is logged with its traceback
and the user sees a generic line pointing at the log.
"""

from __future__ import annotations

from loguru import logger

from grimoire.gui.errors import GuiError

GENERIC_ERROR = "Unexpected error. See the log file for details."


def user_message(exc: BaseException) -> str:
    """Text for ``exc`` that is safe to display.

    Args:
        exc: Whatever a worker caught.

    Returns:
        The error's own message for a ``GuiError``; otherwise ``GENERIC_ERROR``,
        after logging the exception with its traceback.  The exception's text
        is deliberately never returned: it can contain file paths, SQL, or
        other internals.
    """
    if isinstance(exc, GuiError):
        return exc.message
    logger.opt(exception=exc).error("Unexpected error in a TUI worker")
    return GENERIC_ERROR
