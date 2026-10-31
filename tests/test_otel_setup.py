"""OpenTelemetry setup must fail loudly but never block startup.

Both instrumentation blocks used ``except Exception: pass``, so a broken
tracing setup produced no spans and no log line. They now log a warning.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator

import pytest
from loguru import logger

settings_mod = importlib.import_module("grimoire.config.settings")
config_pkg = importlib.import_module("grimoire.config")


@pytest.fixture
def tracing_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn tracing on and make every OTel instrumentation import fail."""
    base = settings_mod.get_settings()
    patched = base.model_copy(
        update={
            "observability": base.observability.model_copy(update={"tracing": True})
        }
    )
    monkeypatch.setattr(settings_mod, "get_settings", lambda: patched)
    monkeypatch.setattr(config_pkg, "get_settings", lambda: patched)
    # A None entry in sys.modules makes `import x` raise ImportError.
    for name in (
        "opentelemetry.instrumentation.fastapi",
        "opentelemetry.instrumentation.sqlalchemy",
    ):
        monkeypatch.setitem(sys.modules, name, None)


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    yield messages
    logger.remove(sink_id)


@pytest.mark.usefixtures("tracing_enabled")
def test_fastapi_instrumentation_failure_is_logged(warnings: list[str]) -> None:
    from grimoire.api.main import create_app

    app = create_app(use_lifespan=False)  # must not raise
    assert app is not None
    assert any("OpenTelemetry FastAPI instrumentation failed" in m for m in warnings)


@pytest.mark.usefixtures("tracing_enabled")
async def test_sqlalchemy_instrumentation_failure_is_logged(
    warnings: list[str],
) -> None:
    from grimoire.db.session import DatabaseSessionManager

    manager = DatabaseSessionManager()
    # create_async_engine does not connect, so no database is needed.
    await manager.initialize("postgresql+asyncpg://u:p@127.0.0.1:1/db")
    try:
        assert manager._session_maker is not None  # startup carried on
        assert any(
            "OpenTelemetry SQLAlchemy instrumentation failed" in m for m in warnings
        )
    finally:
        await manager.close()
