"""Compatibility shim: the errors now live in ``grimoire.client.errors``."""

from grimoire.client.errors import (
    AuthFailed,
    ConnectionFailed,
    GuiError,
    MalformedResponse,
    RateLimited,
    RequestRejected,
    ServerError,
    TimedOut,
)

__all__ = [
    "AuthFailed",
    "ConnectionFailed",
    "GuiError",
    "MalformedResponse",
    "RateLimited",
    "RequestRejected",
    "ServerError",
    "TimedOut",
]
