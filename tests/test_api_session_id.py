"""Tests for the server side of ``X-Session-Id`` (A2)."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from grimoire.api.session_id import (
    SessionIdMiddleware,
    current_session_id,
    parse_session_id,
)
from grimoire.mcp import mlflow_logging
from grimoire.utils.logger import DEFAULT_LOG_FORMAT, setup_logger


class TestParseSessionId:
    @pytest.mark.parametrize("raw", ["abc", "A-b_9", "x" * 64, b"launch-1", "0"])
    def test_valid(self, raw: str | bytes) -> None:
        assert parse_session_id(raw) == (
            raw.decode() if isinstance(raw, bytes) else raw
        )

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "x" * 65,
            "abc\\n",  # literal backslash-n is not allowed either
            "abc\n",  # `$` would accept this; fullmatch must not
            "a b",
            "a|b",
            "a.b",
            "é",
            b"\xff\xfe",
        ],
    )
    def test_invalid(self, raw: str | bytes | None) -> None:
        assert parse_session_id(raw) is None


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionIdMiddleware)

    @app.get("/probe")
    def probe() -> dict[str, str | None]:
        logger.info("inside handler")
        return {"seen": current_session_id.get()}

    return app


@pytest.fixture
def log_lines() -> Iterator[list[str]]:
    """Capture formatted log lines using the project's real log format."""
    lines: list[str] = []
    setup_logger()  # installs the default-extra patcher
    handler_id = logger.add(
        lines.append, format=DEFAULT_LOG_FORMAT + "\n", level="INFO"
    )
    yield lines
    logger.remove(handler_id)


class TestMiddleware:
    def test_valid_header_is_visible_to_handler_and_logs(
        self, log_lines: list[str]
    ) -> None:
        with TestClient(_app()) as c:
            resp = c.get("/probe", headers={"X-Session-Id": "sess-1"})
        assert resp.json() == {"seen": "sess-1"}
        assert any(
            "| sess-1 |" in line and "inside handler" in line for line in log_lines
        )

    @pytest.mark.parametrize("value", ["bad value", "x" * 65, "a|b", ""])
    def test_invalid_header_is_ignored_not_rejected(
        self, value: str, log_lines: list[str]
    ) -> None:
        with TestClient(_app()) as c:
            resp = c.get("/probe", headers={"X-Session-Id": value})
        assert resp.status_code == 200
        assert resp.json() == {"seen": None}
        assert any("| - |" in line and "inside handler" in line for line in log_lines)

    def test_missing_header(self) -> None:
        with TestClient(_app()) as c:
            assert c.get("/probe").json() == {"seen": None}

    def test_does_not_leak_between_requests(self) -> None:
        with TestClient(_app()) as c:
            c.get("/probe", headers={"X-Session-Id": "first"})
            assert c.get("/probe").json() == {"seen": None}
        assert current_session_id.get() is None

    def test_first_of_repeated_headers_wins(self) -> None:
        with TestClient(_app()) as c:
            resp = c.get(
                "/probe", headers=[("X-Session-Id", "one"), ("X-Session-Id", "two")]
            )
        assert resp.json() == {"seen": "one"}

    def test_non_http_scope_is_passed_through(self) -> None:
        import asyncio

        seen: list[str] = []

        async def inner(scope: dict, receive: object, send: object) -> None:
            seen.append(scope["type"])

        mw = SessionIdMiddleware(inner)  # type: ignore[arg-type]
        asyncio.run(mw({"type": "lifespan"}, MagicMock(), MagicMock()))  # type: ignore[arg-type]
        assert seen == ["lifespan"]

    def test_context_is_reset_in_the_calling_context(self) -> None:
        """A server may reuse a context between requests; nothing may linger.

        TestClient runs each request in its own task, which hides a missing
        ``reset``, so call the middleware directly inside one task.
        """
        import asyncio

        async def run() -> tuple[str | None, str | None]:
            seen: list[str | None] = []

            async def inner(scope: dict, receive: object, send: object) -> None:
                seen.append(current_session_id.get())

            mw = SessionIdMiddleware(inner)  # type: ignore[arg-type]
            scope = {"type": "http", "headers": [(b"x-session-id", b"abc")]}
            await mw(scope, MagicMock(), MagicMock())  # type: ignore[arg-type]
            return seen[0], current_session_id.get()

        assert asyncio.run(run()) == ("abc", None)


class TestRealApp:
    def test_create_app_installs_the_middleware(self) -> None:
        from grimoire.api.main import create_app

        app = create_app(use_lifespan=False)
        assert SessionIdMiddleware in [m.cls for m in app.user_middleware]


class TestLogFormat:
    def test_record_without_session_id_gets_dash(self) -> None:
        lines: list[str] = []
        setup_logger()
        hid = logger.add(lines.append, format=DEFAULT_LOG_FORMAT + "\n")
        try:
            logger.info("plain")
        finally:
            logger.remove(hid)
        assert any("| - |" in line and "plain" in line for line in lines)

    def test_setup_logger_does_not_clobber_a_configured_session_id(self) -> None:
        lines: list[str] = []
        logger.configure(extra={"session_id": "launch-9"})
        try:
            setup_logger()
            hid = logger.add(lines.append, format=DEFAULT_LOG_FORMAT + "\n")
            try:
                logger.info("kept")
            finally:
                logger.remove(hid)
        finally:
            logger.configure(extra={})
        assert any("| launch-9 |" in line and "kept" in line for line in lines)


class TestMlflowTag:
    def test_tag_added_when_session_bound(self) -> None:
        mock_mlflow = MagicMock()
        token = current_session_id.set("sess-7")
        try:
            with (
                patch.object(mlflow_logging, "_MLFLOW_AVAILABLE", True),
                patch.object(mlflow_logging, "_mlflow_configured", True),
                patch.object(mlflow_logging, "mlflow", mock_mlflow),
            ):
                mlflow_logging._attach_session_tag()
        finally:
            current_session_id.reset(token)
        mock_mlflow.update_current_trace.assert_called_once_with(
            tags={"grimoire.session_id": "sess-7"}
        )

    def test_no_tag_without_session(self) -> None:
        mock_mlflow = MagicMock()
        with (
            patch.object(mlflow_logging, "_MLFLOW_AVAILABLE", True),
            patch.object(mlflow_logging, "_mlflow_configured", True),
            patch.object(mlflow_logging, "mlflow", mock_mlflow),
        ):
            mlflow_logging._attach_session_tag()
        mock_mlflow.update_current_trace.assert_not_called()

    def test_inactive_mlflow_is_a_noop(self) -> None:
        mock_mlflow = MagicMock()
        token = current_session_id.set("sess-7")
        try:
            with (
                patch.object(mlflow_logging, "_mlflow_configured", False),
                patch.object(mlflow_logging, "mlflow", mock_mlflow),
            ):
                mlflow_logging._attach_session_tag()
        finally:
            current_session_id.reset(token)
        mock_mlflow.update_current_trace.assert_not_called()
