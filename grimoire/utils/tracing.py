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

MLflow tracing for the MCP server is separate; see ``grimoire.mcp.mlflow_logging``.
"""

from __future__ import annotations

import os
import threading

from loguru import logger

_lock = threading.Lock()
_configured = False


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

        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
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
        logger.info(f"OpenTelemetry tracing to {endpoint} as service {service!r}")
        return True


def _reset_for_tests() -> None:
    """Forget that tracing was configured (tests only)."""
    global _configured
    with _lock:
        _configured = False
