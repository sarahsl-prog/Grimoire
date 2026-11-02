"""grimoire.utils.tracing: a real tracer provider when tracing is enabled.

Before this, ``observability.tracing: true`` instrumented FastAPI and
SQLAlchemy against OpenTelemetry's default no-op provider, so no span was
ever exported (audit finding C-8).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from loguru import logger
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from grimoire.utils import tracing

_EXPORTER = "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"
_HTTP_EXPORTER = (
    "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Capture the provider instead of installing it process-wide."""
    tracing._reset_for_tests()
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raising=False)
    installed: list[Any] = []
    with (
        patch("opentelemetry.trace.set_tracer_provider", installed.append),
        patch(
            "opentelemetry.instrumentation.redis.RedisInstrumentor.instrument"
        ) as redis_instrument,
    ):
        installed.append(redis_instrument)  # index 0: the Redis mock
        yield installed
    tracing._reset_for_tests()


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    yield messages
    logger.remove(sink)


def test_without_endpoint_warns_and_installs_nothing(
    _isolated: list[Any], warnings: list[str]
) -> None:
    assert tracing.configure_tracing() is False
    assert len(_isolated) == 1  # only the Redis mock; no provider installed
    assert any("OTEL_EXPORTER_OTLP_ENDPOINT is not set" in m for m in warnings)


def test_with_endpoint_spans_are_exported(
    _isolated: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "grimoire-api")
    exporter = InMemorySpanExporter()
    with patch(_EXPORTER, return_value=exporter):
        assert tracing.configure_tracing() is True

    redis_instrument, provider = _isolated
    assert isinstance(provider, TracerProvider)
    redis_instrument.assert_called_once()

    with provider.get_tracer("test").start_as_current_span("search"):
        pass
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["search"]
    assert spans[0].resource.attributes["service.name"] == "grimoire-api"


def test_is_idempotent(_isolated: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    with patch(_EXPORTER, return_value=InMemorySpanExporter()):
        assert tracing.configure_tracing() is True
        assert tracing.configure_tracing() is True
    providers = [p for p in _isolated if isinstance(p, TracerProvider)]
    assert len(providers) == 1


def test_default_protocol_uses_grpc_exporter(
    _isolated: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    with (
        patch(_EXPORTER, return_value=InMemorySpanExporter()) as grpc_exporter,
        patch(_HTTP_EXPORTER) as http_exporter,
    ):
        assert tracing.configure_tracing() is True
    grpc_exporter.assert_called_once()
    http_exporter.assert_not_called()


@pytest.mark.parametrize(
    "variable", ["OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"]
)
def test_http_protobuf_uses_http_exporter(
    _isolated: list[Any], monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """MLflow only accepts OTLP over HTTP, so the protocol must be selectable."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://mlflow:5000")
    monkeypatch.setenv(variable, " HTTP/Protobuf ")
    with (
        patch(_EXPORTER) as grpc_exporter,
        patch(_HTTP_EXPORTER, return_value=InMemorySpanExporter()) as http_exporter,
    ):
        assert tracing.configure_tracing() is True
    http_exporter.assert_called_once()
    grpc_exporter.assert_not_called()


def test_traces_protocol_overrides_generic(
    _isolated: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://mlflow:5000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")
    with patch(_HTTP_EXPORTER, return_value=InMemorySpanExporter()) as http_exporter:
        assert tracing.configure_tracing() is True
    http_exporter.assert_called_once()


def test_invalid_traces_protocol_does_not_fall_back_to_generic(
    _isolated: list[Any], monkeypatch: pytest.MonkeyPatch, warnings: list[str]
) -> None:
    """The traces-specific variable is authoritative: a bad value disables export
    rather than silently using the generic one, so the misconfiguration is seen."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/json")
    with patch(_EXPORTER) as grpc_exporter:
        assert tracing.configure_tracing() is False
    grpc_exporter.assert_not_called()
    assert any("Unsupported OTLP protocol 'http/json'" in m for m in warnings)


def test_empty_traces_protocol_falls_through_to_generic(
    _isolated: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://mlflow:5000")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    with patch(_HTTP_EXPORTER, return_value=InMemorySpanExporter()) as http_exporter:
        assert tracing.configure_tracing() is True
    http_exporter.assert_called_once()


@pytest.mark.parametrize("protocol", ["http/json", "websocket", "http"])
def test_unsupported_protocol_warns_and_installs_nothing(
    _isolated: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[str],
    protocol: str,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    with patch(_EXPORTER) as grpc_exporter, patch(_HTTP_EXPORTER) as http_exporter:
        assert tracing.configure_tracing() is False
    grpc_exporter.assert_not_called()
    http_exporter.assert_not_called()
    assert len(_isolated) == 1  # only the Redis mock; no provider installed
    assert any("Unsupported OTLP protocol" in m for m in warnings)


def test_setup_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, warnings: list[str]
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    with patch(_EXPORTER, side_effect=RuntimeError("bad TLS config")):
        assert tracing.configure_tracing() is False
    assert any("OpenTelemetry setup failed" in m for m in warnings)
