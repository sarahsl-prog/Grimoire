"""Tests for the standalone MCP ASGI application.

The standalone SSE transport used to serve MCP with no authentication at
all, which exposed every unguarded read tool to anyone who could reach the
port.  These tests pin the requirement that it now enforces the same
X-API-Key check as the REST-mounted endpoint.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from grimoire.mcp.app import create_mcp_app


@pytest.fixture
def client() -> TestClient:
    """Standalone MCP app with the DB lifespan disabled."""
    return TestClient(create_mcp_app(use_lifespan=False))


def test_health_endpoint_is_unauthenticated(client: TestClient) -> None:
    """Container healthchecks must work without an API key."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mcp_mounted_at_expected_path(client: TestClient) -> None:
    paths = [r.path for r in client.app.routes if isinstance(r, Mount)]
    assert "/mcp" in paths


def test_sse_requires_api_key(client: TestClient) -> None:
    """No X-API-Key header means no access, on the standalone transport too."""
    response = client.get("/mcp/sse")
    assert response.status_code == 401


def test_sse_rejects_invalid_api_key(client: TestClient) -> None:
    """An unknown key never reaches the tools.

    Without a real database the middleware returns 503; what matters is that
    it is not a 200.
    """
    with patch("grimoire.db.session.initialize_db", new_callable=AsyncMock):
        response = client.get("/mcp/sse", headers={"X-API-Key": "grim_rdl_invalidkey"})
    assert response.status_code in {401, 403, 503}
    assert response.status_code != 200


def test_cli_sse_serves_the_authenticated_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """`grimoire mcp --sse` must serve the mounted app, not a raw sse_app()."""
    from click.testing import CliRunner

    from grimoire.cli.mcp import mcp as mcp_command

    served: dict[str, Any] = {}

    class _FakeServer:
        def __init__(self, config: Any) -> None:
            served["app"] = config.app

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("uvicorn.Server", _FakeServer)

    result = CliRunner().invoke(mcp_command, ["--sse", "--port", "8100"])
    assert result.exit_code == 0, result.output

    app = served["app"]
    mounts = [r.path for r in app.routes if isinstance(r, Mount)]
    assert "/mcp" in mounts
