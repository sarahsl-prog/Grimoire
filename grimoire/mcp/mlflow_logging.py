"""MLflow tracing integration for the Grimoire MCP server.

When enabled via ``observability.mlflow_enabled``, each MCP tool invocation
is recorded as an MLflow trace span (span type ``TOOL``).  Downstream LLM
calls made by tools can also be captured when ``mlflow_langchain_autolog`` is
set.

MLflow is an optional dependency — install with ``pip install 'grimoire[mlflow]'``.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Callable, TypeVar

from loguru import logger

from grimoire.config.settings import ObservabilityConfig

try:
    import mlflow
    from mlflow.entities import SpanType

    _MLFLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via tests with patching
    mlflow = None  # type: ignore[assignment]
    SpanType = None  # type: ignore[assignment,misc]
    _MLFLOW_AVAILABLE = False

F = TypeVar("F", bound=Callable[..., Any])

_mlflow_configured = False


def is_mlflow_active() -> bool:
    """Return True when MLflow tracing has been configured for this process."""
    return _mlflow_configured


def configure_mlflow(config: ObservabilityConfig) -> bool:
    """Initialise MLflow tracing for the MCP server.

    Returns:
        True when MLflow tracing is active, False otherwise.
    """
    global _mlflow_configured

    if not config.mlflow_enabled:
        _mlflow_configured = False
        return False

    if not _MLFLOW_AVAILABLE:
        logger.warning(
            "observability.mlflow_enabled is true but mlflow is not installed. "
            "Install with: pip install 'grimoire[mlflow]'"
        )
        return False

    if config.mlflow_tracking_uri:
        mlflow.set_tracking_uri(config.mlflow_tracking_uri)

    mlflow.set_experiment(config.mlflow_experiment_name)

    if config.mlflow_langchain_autolog:
        try:
            mlflow.langchain.autolog()
            logger.info("MLflow LangChain autolog enabled for MCP tool LLM calls")
        except Exception as e:
            logger.warning(f"MLflow LangChain autolog could not be enabled: {e}")

    _mlflow_configured = True
    logger.info(
        "MLflow tracing enabled for MCP server "
        f"(experiment={config.mlflow_experiment_name!r})"
    )
    return True


def shutdown_mlflow() -> None:
    """Flush pending MLflow trace exports on MCP server shutdown."""
    global _mlflow_configured
    if not _mlflow_configured or not _MLFLOW_AVAILABLE:
        return
    try:
        mlflow.flush_trace_async_logging()
    except Exception as e:
        logger.debug(f"MLflow trace flush skipped: {e}")
    _mlflow_configured = False


def _serialize_tool_input(value: Any) -> Any:
    """Convert tool parameters to a JSON-safe structure for trace inputs."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _serialize_tool_input(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_tool_input(v) for v in value]
    return repr(value)


def _attach_api_key_tags() -> None:
    """Add API key metadata to the active trace when available."""
    if not _MLFLOW_AVAILABLE or not _mlflow_configured:
        return
    try:
        from .auth_stdio import _stdio_api_key

        api_key = _stdio_api_key.get()
        if api_key is None:
            return
        mlflow.update_current_trace(
            tags={
                "grimoire.api_key_tier": api_key.tier.value,
                "grimoire.api_key_name": api_key.name,
            }
        )
    except Exception:
        return


def _summarize_tool_output(result: Any) -> dict[str, Any]:
    """Build a compact trace output payload from a tool response string."""
    if not isinstance(result, str):
        return {"result": _serialize_tool_input(result)}

    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return {"result_preview": result[:500]}

    summary: dict[str, Any] = {"status": payload.get("status")}
    if payload.get("status") == "error":
        summary["message"] = payload.get("message")
    elif "data" in payload:
        data = payload["data"]
        if isinstance(data, dict):
            summary["data_keys"] = list(data.keys())
        else:
            summary["data_type"] = type(data).__name__
    return summary


def trace_mcp_tool(func: F, *, name: str) -> F:
    """Wrap an MCP tool with MLflow tracing when configured."""
    if not _mlflow_configured or not _MLFLOW_AVAILABLE:

        @functools.wraps(func)
        async def passthrough(*args: Any, **kwargs: Any) -> Any:
            return await func(*args, **kwargs)

        return passthrough  # type: ignore[return-value]

    @mlflow.trace(
        name=name,
        span_type=SpanType.TOOL,
        attributes={"grimoire.mcp_tool": name},
    )
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        _attach_api_key_tags()
        if args:
            mlflow.update_current_trace(
                metadata={"grimoire.tool_input": _serialize_tool_input(args[0])}
            )
        result = await func(*args, **kwargs)
        mlflow.update_current_trace(
            metadata={"grimoire.tool_output": _summarize_tool_output(result)}
        )
        return result

    return wrapper  # type: ignore[return-value]
