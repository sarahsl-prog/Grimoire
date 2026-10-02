"""Turn exceptions into text that is safe to show on screen.

The project rule is that raw exceptions never reach the user.  The API client
already raises only ``ClientError`` subclasses, each carrying a complete sentence
written for a person.  Anything else is a bug: it is logged with its traceback
and the user sees a generic line pointing at the log.
"""

from __future__ import annotations

from loguru import logger

from grimoire.client.errors import ClientError, ConnectionFailed, TimedOut

GENERIC_ERROR = "Unexpected error. See the log file for details."


def user_message(exc: BaseException) -> str:
    """Text for ``exc`` that is safe to display.

    Args:
        exc: Whatever a worker caught.

    Returns:
        The error's own message for a ``ClientError``; otherwise ``GENERIC_ERROR``,
        after logging the exception with its traceback.  The exception's text
        is deliberately never returned: it can contain file paths, SQL, or
        other internals.
    """
    if isinstance(exc, ClientError):
        return exc.message
    logger.opt(exception=exc).error("Unexpected error in a TUI worker")
    return GENERIC_ERROR


def reachability(error: BaseException) -> bool | None:
    """What a failed request says about whether the API is up.

    Panes use this to keep the status bar honest between health checks.

    Returns:
        False for a connection failure (the API is down).  True for any other
        client error: a 401, 429, 500 or unreadable body is still an *answer*,
        so the API is up.  None when it proves nothing either way: a timeout
        (a slow server and a dead one look alike) or an unexpected exception.
    """
    if isinstance(error, ConnectionFailed):
        return False
    if isinstance(error, TimedOut):
        return None
    if isinstance(error, ClientError):
        return True
    return None
