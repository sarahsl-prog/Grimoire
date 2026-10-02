"""Error types surfaced to the GUI.

Widgets only ever see these.  Every httpx exception, HTTP status, and
validation failure is translated here into text a person can act on; the
original exception goes to the log, never to the screen.
"""

from __future__ import annotations


class GuiError(Exception):
    """Base class for every failure the GUI is expected to display.

    Attributes:
        message: Text to show the user.  Complete sentence, no traceback.
        retry_after: Seconds to wait before retrying, when the server said.
    """

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class ConnectionFailed(GuiError):
    """The API could not be reached at all."""


class TimedOut(GuiError):
    """The API accepted the connection but did not answer in time."""


class AuthFailed(GuiError):
    """The API key is missing or rejected."""


class RequestRejected(GuiError):
    """The API refused this particular request (403, 404, 413, 415, 422)."""


class RateLimited(GuiError):
    """The API key's rate limit is exhausted."""


class ServerError(GuiError):
    """The API failed internally."""


class MalformedResponse(GuiError):
    """The API answered with something this client cannot parse."""
