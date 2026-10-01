"""Tests for turning exceptions into text safe to show on screen."""

from __future__ import annotations

import pytest
from loguru import logger

from grimoire.gui.errors import (
    AuthFailed,
    ConnectionFailed,
    GuiError,
    MalformedResponse,
    RateLimited,
    RequestRejected,
    ServerError,
    TimedOut,
)
from grimoire.tui.errors import GENERIC_ERROR, user_message


@pytest.mark.parametrize(
    "exc_type",
    [
        AuthFailed,
        ConnectionFailed,
        MalformedResponse,
        RateLimited,
        RequestRejected,
        ServerError,
        TimedOut,
        GuiError,
    ],
)
def test_gui_errors_show_their_own_message(exc_type: type[GuiError]) -> None:
    assert user_message(exc_type("Something a person can act on.")) == (
        "Something a person can act on."
    )


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("secret internal detail /srv/app/db.py"),
        KeyError("boom"),
        ValueError(""),
    ],
)
def test_other_exceptions_get_the_generic_message_and_never_leak(
    exc: Exception,
) -> None:
    text = user_message(exc)

    assert text == GENERIC_ERROR
    assert str(exc) not in text or str(exc) == ""


def test_other_exceptions_are_logged_with_their_traceback() -> None:
    records: list[str] = []
    sink_id = logger.add(records.append, format="{message}", level="ERROR")
    try:
        try:
            raise RuntimeError("secret internal detail")
        except RuntimeError as exc:
            user_message(exc)
    finally:
        logger.remove(sink_id)

    joined = "".join(records)
    assert "secret internal detail" in joined
    assert "RuntimeError" in joined


def test_gui_errors_are_not_logged_as_unexpected() -> None:
    records: list[str] = []
    sink_id = logger.add(records.append, format="{message}", level="ERROR")
    try:
        user_message(ConnectionFailed("Cannot reach the API."))
    finally:
        logger.remove(sink_id)

    assert records == []
