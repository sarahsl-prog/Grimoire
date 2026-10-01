"""Messages shared between the app and its panes.

Kept in their own module so a pane can post one without importing the app,
which would be a circular import.
"""

from __future__ import annotations

from textual.message import Message


class ConnectionReport(Message):
    """The outcome of an attempt to reach the API.

    Posted by the app's own health check, and by any pane after a request: a
    pane that just got an answer knows the API is up, and one that just got a
    connection error knows it is not, without waiting for the next health
    check.  Bubbles to the app, which updates the status bar.

    Attributes:
        reachable: True if the API answered, False if it could not be reached.
    """

    def __init__(self, reachable: bool) -> None:
        super().__init__()
        self.reachable = reachable
