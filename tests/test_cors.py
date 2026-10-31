"""CORS configuration tests.

None of Grimoire's clients are browsers on another origin, so the default
must not send any ``Access-Control-Allow-Origin`` header. The validator must
catch the two misconfigurations Starlette accepts silently: an origin with a
path or trailing slash (never matches) and ``*`` mixed with real origins
(allows everything).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from grimoire.config.settings import AuthConfig

# `grimoire.config` exports a `settings` object that shadows the submodule
# name, so `import grimoire.config.settings as m` binds the object instead.
settings_mod = importlib.import_module("grimoire.config.settings")

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIL = "https://evil.example.com"
APP_ORIGIN = "https://app.example.com"


class TestCorsOriginValidation:
    def test_default_is_empty(self) -> None:
        assert AuthConfig().cors_origins == []

    @pytest.mark.parametrize(
        "origins",
        [
            [],
            ["https://app.example.com"],
            ["http://localhost:3000", "https://grimoire.example.com"],
            ["http://127.0.0.1:8001"],
            ["*"],
        ],
    )
    def test_accepts_valid_origins(self, origins: list[str]) -> None:
        assert AuthConfig(cors_origins=origins).cors_origins == origins

    @pytest.mark.parametrize(
        "bad",
        [
            "https://app.example.com/",  # trailing slash never matches
            "https://app.example.com/ui",  # origins have no path
            "app.example.com",  # no scheme
            "ftp://app.example.com",
            "https://app.example.com:99999x",
            "",
            " https://app.example.com",
        ],
    )
    def test_rejects_non_origins(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="is not an origin"):
            AuthConfig(cors_origins=[bad])

    def test_rejects_wildcard_mixed_with_origins(self) -> None:
        with pytest.raises(ValidationError, match="must be the only entry"):
            AuthConfig(cors_origins=["*", APP_ORIGIN])

    def test_wildcard_alone_logs_a_warning(self) -> None:
        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            AuthConfig(cors_origins=["*"])
        finally:
            logger.remove(sink_id)
        assert any("CORS allows any origin" in m for m in messages)


def _client(monkeypatch: pytest.MonkeyPatch, origins: list[str]) -> TestClient:
    """Build the app with ``auth.cors_origins`` overridden."""
    base = settings_mod.get_settings()
    patched = base.model_copy(
        update={"auth": base.auth.model_copy(update={"cors_origins": origins})}
    )
    monkeypatch.setattr(settings_mod, "get_settings", lambda: patched)
    from grimoire.api.main import create_app

    app = create_app(use_lifespan=False)
    if getattr(app.state, "limiter", None):
        app.state.limiter.enabled = False
    return TestClient(app, raise_server_exceptions=False)


def _preflight(client: TestClient, origin: str) -> Any:
    return client.options(
        "/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )


class TestCorsMiddleware:
    @pytest.fixture
    def default_client(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
        with _client(monkeypatch, []) as c:
            yield c

    def test_default_sends_no_allow_origin(self, default_client: TestClient) -> None:
        resp = default_client.get("/health", headers={"Origin": EVIL})
        assert "access-control-allow-origin" not in resp.headers

    def test_default_rejects_preflight(self, default_client: TestClient) -> None:
        resp = _preflight(default_client, EVIL)
        assert "access-control-allow-origin" not in resp.headers

    def test_same_origin_requests_still_work(self, default_client: TestClient) -> None:
        """Swagger UI and non-browser clients don't send a foreign Origin."""
        assert default_client.get("/health").status_code == 200

    def test_listed_origin_is_allowed_and_others_are_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _client(monkeypatch, [APP_ORIGIN]) as c:
            ok = _preflight(c, APP_ORIGIN)
            assert ok.headers.get("access-control-allow-origin") == APP_ORIGIN
            denied = _preflight(c, EVIL)
            assert "access-control-allow-origin" not in denied.headers


class TestEnvTemplates:
    """`cp .env.example .env` must not re-open what the code default closes."""

    @pytest.mark.parametrize("name", [".env.example", ".env.security.example"])
    def test_templates_do_not_allow_every_origin(self, name: str) -> None:
        lines = (REPO_ROOT / name).read_text(encoding="utf-8").splitlines()
        cors = [ln for ln in lines if ln.startswith("GRIMOIRE_AUTH__CORS_ORIGINS=")]
        assert cors == ["GRIMOIRE_AUTH__CORS_ORIGINS=[]"], cors
