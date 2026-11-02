"""OpenTelemetry tracer setup.

``observability.tracing: true`` instrumented FastAPI and SQLAlchemy but never
installed a tracer provider, so the default no-op provider swallowed every
span (audit finding C-8). :func:`configure_tracing` installs a real provider
with an OTLP exporter, once per process, before any instrumentation runs.

Configuration uses the standard OpenTelemetry environment variables, which
``.env.example`` already documents:

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` — collector address, e.g.
  ``http://otel-collector:4317`` (OTLP over gRPC). Required: with tracing on
  and no endpoint, nothing is exported and a warning says so, rather than
  spending every request on spans sent to a default address that isn't
  listening.
* ``OTEL_SERVICE_NAME`` — service name on every span (default ``grimoire``).
* ``OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`` / ``OTEL_EXPORTER_OTLP_PROTOCOL`` —
  ``grpc`` (default) or ``http/protobuf``. Some backends, MLflow among them,
  only accept OTLP over HTTP and reject the gRPC connection preface.

MLflow tracing for the MCP server is separate; see ``grimoire.mcp.mlflow_logging``.
"""

from __future__ import annotations

import os
import threading

from loguru import logger

_lock = threading.Lock()
_configured = False

# The values the OpenTelemetry spec defines for OTEL_EXPORTER_OTLP_PROTOCOL
# that the Python SDK can export with. ``http/json`` is in the spec but has no
# Python exporter.
_SUPPORTED_PROTOCOLS = ("grpc", "http/protobuf")


def _otlp_protocol() -> str | None:
    """Return the configured OTLP protocol, or None if it is unsupported.

    The traces-specific variable wins over the generic one, as the spec says.
    """
    raw = (
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL")
        or os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
        or "grpc"
    )
    protocol = raw.strip().lower()
    if protocol not in _SUPPORTED_PROTOCOLS:
        logger.warning(
            f"Unsupported OTLP protocol {raw!r}; expected one of "
            f"{', '.join(_SUPPORTED_PROTOCOLS)}. Spans will not be exported"
        )
        return None
    return protocol


def configure_tracing(default_service_name: str = "grimoire") -> bool:
    """Install the global tracer provider. Idempotent and never raises.

    Call it before instrumenting anything. Safe to call from every entry
    point (API app, MCP app, database initialisation); only the first call
    in a process does the work.

    Args:
        default_service_name: Used when ``OTEL_SERVICE_NAME`` is unset.

    Returns:
        True if spans will be exported (now or by an earlier call).
    """
    global _configured
    with _lock:
        if _configured:
            return True

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if not endpoint:
            logger.warning(
                "Tracing is enabled but OTEL_EXPORTER_OTLP_ENDPOINT is not set; "
                "spans will not be exported"
            )
            return False

        protocol = _otlp_protocol()
        if protocol is None:
            return False

        try:
            from opentelemetry import trace

            # Both exporters are named OTLPSpanExporter and read the same
            # OTEL_EXPORTER_OTLP_* variables; the HTTP one appends /v1/traces
            # to a generic endpoint itself.
            if protocol == "http/protobuf":
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
            else:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[assignment]
                    OTLPSpanExporter,
                )
            from opentelemetry.sdk.resources import SERVICE_NAME, Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            service = os.environ.get("OTEL_SERVICE_NAME") or default_service_name
            provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service}))
            # The exporter reads the endpoint (and any OTEL_EXPORTER_OTLP_*
            # headers/TLS settings) from the environment itself.
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)

            # Redis calls (cache, rate limiter) as child spans. The package is
            # a declared dependency; it was installed but never used.
            from opentelemetry.instrumentation.redis import RedisInstrumentor

            RedisInstrumentor().instrument()
        except Exception as e:
            logger.warning(f"OpenTelemetry setup failed: {type(e).__name__}: {e}")
            return False

        _configured = True
        logger.info(
            f"OpenTelemetry tracing to {endpoint} ({protocol}) as service {service!r}"
        )
        return True


def _reset_for_tests() -> None:
    """Forget that tracing was configured (tests only)."""
    global _configured
    with _lock:
        _configured = False
