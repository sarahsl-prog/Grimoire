"""``grimoire mcp --sse`` binds loopback unless told otherwise."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from grimoire.cli.mcp import mcp as mcp_command


def _capture_host(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    class _FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    class _FakeServer:
        def __init__(self, config: Any) -> None:
            pass

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("uvicorn.Config", _FakeConfig)
    monkeypatch.setattr("uvicorn.Server", _FakeServer)
    return seen


def test_sse_binds_loopback_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_host(monkeypatch)
    result = CliRunner().invoke(mcp_command, ["--sse"])
    assert result.exit_code == 0, result.output
    assert seen["host"] == "127.0.0.1"


def test_sse_host_can_still_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_host(monkeypatch)
    wide_open = "0.0.0.0"  # noqa: S104  # nosec B104
    result = CliRunner().invoke(mcp_command, ["--sse", "--host", wide_open])
    assert result.exit_code == 0, result.output
    assert seen["host"] == wide_open
