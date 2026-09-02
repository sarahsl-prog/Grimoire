"""Tests for MLflow tracing integration in the MCP server."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grimoire.config.settings import ObservabilityConfig
from grimoire.mcp import mlflow_logging
from grimoire.mcp.server import create_mcp_server


@pytest.fixture
def mcp_server() -> object:
    """Create a test MCP server."""
    return create_mcp_server()


@pytest.fixture(autouse=True)
def reset_mlflow_state() -> None:
    """Reset module-level MLflow state between tests."""
    mlflow_logging._mlflow_configured = False
    yield
    mlflow_logging._mlflow_configured = False


def test_configure_mlflow_disabled_by_default() -> None:
    """MLflow is not configured when mlflow_enabled is false."""
    config = ObservabilityConfig()
    assert mlflow_logging.configure_mlflow(config) is False
    assert mlflow_logging.is_mlflow_active() is False


def test_configure_mlflow_warns_when_package_missing() -> None:
    """MLflow stays inactive when enabled but the package is not installed."""
    config = ObservabilityConfig(mlflow_enabled=True)

    with patch.object(mlflow_logging, "_MLFLOW_AVAILABLE", False):
        assert mlflow_logging.configure_mlflow(config) is False
        assert mlflow_logging.is_mlflow_active() is False


def test_configure_mlflow_sets_experiment() -> None:
    """MLflow experiment and tracking URI are configured when enabled."""
    config = ObservabilityConfig(
        mlflow_enabled=True,
        mlflow_tracking_uri="sqlite:///test.db",
        mlflow_experiment_name="mcp-test",
        mlflow_langchain_autolog=False,
    )
    mock_mlflow = MagicMock()

    with patch.object(mlflow_logging, "_MLFLOW_AVAILABLE", True), \
         patch.object(mlflow_logging, "mlflow", mock_mlflow):
        assert mlflow_logging.configure_mlflow(config) is True
        assert mlflow_logging.is_mlflow_active() is True
        mock_mlflow.set_tracking_uri.assert_called_once_with("sqlite:///test.db")
        mock_mlflow.set_experiment.assert_called_once_with("mcp-test")
        mock_mlflow.langchain.autolog.assert_not_called()


@pytest.mark.asyncio
async def test_trace_mcp_tool_passthrough_when_inactive() -> None:
    """Tools run unchanged when MLflow tracing is not active."""

    async def sample_tool(value: int) -> int:
        return value * 2

    wrapped = mlflow_logging.trace_mcp_tool(sample_tool, name="sample_tool")
    assert await wrapped(3) == 6


@pytest.mark.asyncio
async def test_trace_mcp_tool_wraps_when_active() -> None:
    """Active MLflow config wraps tools with trace metadata updates."""
    mlflow_logging._mlflow_configured = True
    mock_mlflow = MagicMock()

    async def sample_tool(params: object) -> str:
        return '{"status": "ok", "data": {"answer": "hi"}}'

    def identity_trace(**_kwargs: object):
        def decorator(fn: object) -> object:
            return fn
        return decorator

    mock_mlflow.trace.side_effect = identity_trace

    with patch.object(mlflow_logging, "_MLFLOW_AVAILABLE", True), \
         patch.object(mlflow_logging, "mlflow", mock_mlflow), \
         patch.object(mlflow_logging, "_attach_api_key_tags"), \
         patch.object(mlflow_logging, "_summarize_tool_output", return_value={"status": "ok"}):
        wrapped = mlflow_logging.trace_mcp_tool(sample_tool, name="grimoire_ask")
        result = await wrapped(MagicMock())

    assert result == '{"status": "ok", "data": {"answer": "hi"}}'
    assert mock_mlflow.update_current_trace.call_count == 2


def test_serialize_tool_input_handles_pydantic() -> None:
    """Pydantic models are serialized for trace metadata."""
    from grimoire.mcp.tools import SearchInput

    payload = mlflow_logging._serialize_tool_input(SearchInput(query="test"))
    assert payload == {"query": "test", "top_k": 10, "filter_dict": None}


def test_summarize_tool_output_error() -> None:
    """Error responses are summarized without dumping full payloads."""
    summary = mlflow_logging._summarize_tool_output(
        '{"status": "error", "message": "not found"}'
    )
    assert summary == {"status": "error", "message": "not found"}


@pytest.mark.asyncio
async def test_lifespan_configures_mlflow(mcp_server: object) -> None:
    """MCP lifespan calls configure_mlflow on startup and shutdown."""
    with patch("grimoire.mcp.server.initialize_db", new_callable=AsyncMock) as mock_init, \
         patch("grimoire.mcp.server.close_db", new_callable=AsyncMock) as mock_close, \
         patch("grimoire.mcp.server.configure_mlflow") as mock_configure, \
         patch("grimoire.mcp.server.shutdown_mlflow") as mock_shutdown, \
         patch.dict("os.environ", {}, clear=True):
        async with mcp_server._lowlevel_server.lifespan(None):  # type: ignore[attr-defined]
            mock_configure.assert_called_once()
        mock_shutdown.assert_called_once()
    mock_init.assert_awaited_once()
    mock_close.assert_awaited_once()
