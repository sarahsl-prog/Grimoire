# Containerize the Grimoire App — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the Grimoire application as a container image and define a Compose topology that runs the API, MCP server, and watcher as managed services in any environment.

**Architecture:** One multi-stage `Dockerfile` produces a single image; the `api`, `mcp`, `watcher`, and `db-migrate` services all run it and differ only by `command`. Compose supplies `GRIMOIRE_*__*` environment variables that override `grimoire.yaml`'s localhost defaults with service DNS names, so the bare-metal workflow keeps working unchanged. Two overlay files add GPU acceleration and development bind-mounts, following the pattern already set by `docker-compose.security.yml`.

**Tech Stack:** Docker, Docker Compose v2, `uv`, Python 3.13, FastAPI/uvicorn, Alembic, ChromaDB (HTTP client mode), PostgreSQL, Redis, pytest.

**Spec:** `docs/superpowers/specs/2026-09-17-containerize-app-design.md`

## Global Constraints

- **Python version in images:** `python:3.13-slim`. `pyproject.toml` declares `requires-python = ">=3.12"`; the host venv is 3.13. Do not downgrade.
- **Dependency resolution:** always `--frozen` against the existing `uv.lock`. Never let a build re-resolve dependencies.
- **Torch version:** `2.11.0`, as pinned in `uv.lock`. The CPU and GPU image variants must install the same version.
- **Config precedence (do not fight it):** `grimoire/config/settings.py:1107` orders sources as init → **env vars** → `.env` → secrets → YAML. Environment variables beat `grimoire.yaml`. This is what makes the layering work.
- **`.env` is not read inside containers.** `settings.py:1049` resolves `env_file` relative to the installed package directory, which in the image is inside `/opt/venv/lib/python3.13/site-packages/`. Configuration must reach containers as real environment variables (Compose `environment:` or `env_file:`), never as a mounted `.env` file.
- **YAML config path:** `GRIMOIRE_CONFIG` selects the file, defaulting to a relative `grimoire.yaml` (`settings.py:842`). Containers set it explicitly to `/app/grimoire.yaml`.
- **Ollama URL must not carry a `/v1` suffix.** The agents append `/api/generate` (`grimoire/agents/query.py:422`, `grimoire/agents/content_gen.py:431`, `grimoire/agents/coordinator.py:820`). A `/v1` base produces `/v1/api/generate` and a 404.
- **Commit messages:** imperative, one line, no trailing period (`CLAUDE.md`). Never batch multiple fixes into one commit.
- **Test style:** `pytest` with `asyncio_mode = "auto"`. Deploy tests parse files and must not require a running Docker daemon; gate any test that shells out to a binary behind `shutil.which`, as `tests/deploy/test_compose_overlay.py` already does.
- **Service naming:** the migration service is `db-migrate`, never `migrate`. `grimoire migrate` is an unrelated vector-store CLI stub (`grimoire/cli/migrate.py:45`).

---

### Task 1: Authenticate the standalone MCP SSE transport

This is a security fix and a prerequisite. `grimoire/cli/mcp.py:48` passes `mcp_server.sse_app()` directly to uvicorn, so `grimoire mcp --sse` serves MCP with no authentication. Eight tools carry no `require_tier()` guard — `grimoire_search`, `grimoire_search_cve`, `grimoire_search_playbook`, `grimoire_ask`, `grimoire_get_document`, `grimoire_list_documents`, `grimoire_list_categories`, `grimoire_status` — so any client reaching that port reads the whole corpus. Publishing a container port would make this network-reachable.

The fix reuses `mount_mcp()` (`grimoire/mcp/router.py:37`), which already implements the `X-API-Key` check. A small FastAPI app wraps it, the CLI serves that app, and the same app becomes the `mcp` container's entrypoint. No new authentication code is written.

**Behavior change to note in Task 6:** the SSE endpoint moves from `/sse` to `/mcp/sse`, because the MCP app is now mounted under `/mcp` rather than served at the root.

**Files:**
- Create: `grimoire/mcp/app.py`
- Modify: `grimoire/cli/mcp.py:44-53`
- Test: `tests/test_mcp_app.py`

**Interfaces:**
- Consumes: `mount_mcp(app: FastAPI, path: str = "/mcp") -> None` from `grimoire/mcp/router.py`.
- Produces: `create_mcp_app(use_lifespan: bool = True) -> FastAPI` and a module-level `app: FastAPI` in `grimoire/mcp/app.py`. Task 3 runs it as `uvicorn grimoire.mcp.app:app`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_app.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_mcp_app.py -v`

Expected: FAIL at collection with `ModuleNotFoundError: No module named 'grimoire.mcp.app'`.

- [ ] **Step 3: Create the standalone MCP application**

Create `grimoire/mcp/app.py`:

```python
"""Standalone ASGI application for the Grimoire MCP server.

Serves the MCP tools over SSE behind the same ``X-API-Key`` middleware the
REST API applies, so no transport exposes the corpus unauthenticated.  Run
it with ``uvicorn grimoire.mcp.app:app`` or via ``grimoire mcp --sse``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Open and close the database pool the auth middleware depends on."""
    from grimoire.config.settings import get_settings
    from grimoire.db.session import close_db, initialize_db

    settings = get_settings()
    await initialize_db(settings.database.url)
    try:
        yield
    finally:
        await close_db()


def create_mcp_app(use_lifespan: bool = True) -> FastAPI:
    """Build the FastAPI app that serves only the authenticated MCP endpoint.

    Args:
        use_lifespan: Whether to manage the database pool.  Tests pass False.

    Returns:
        A FastAPI application with MCP mounted at ``/mcp``.
    """
    from grimoire.config.settings import ConfigurationError, get_settings
    from grimoire.mcp.router import mount_mcp

    # Imported as `grimoire.mcp.app:app`, so a config failure surfaces at
    # uvicorn import time.  Exit with a readable message rather than a
    # Pydantic traceback, matching grimoire/api/main.py:49.
    try:
        get_settings()
    except ConfigurationError as e:
        logger.error(f"Cannot start the Grimoire MCP server - invalid configuration:\n{e}")
        raise SystemExit(1) from e

    app = FastAPI(
        title="Grimoire MCP",
        description="Model Context Protocol server for the Grimoire knowledge base.",
        version="2.0.0",
        lifespan=lifespan if use_lifespan else None,
    )

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    mount_mcp(app, path="/mcp")

    return app


app = create_mcp_app()
```

- [ ] **Step 4: Route the CLI's SSE mode through the authenticated app**

In `grimoire/cli/mcp.py`, replace the `elif sse:` branch (currently lines 44-53) with:

```python
    elif sse:
        logger.info(f"Starting Grimoire MCP server (SSE transport on {host}:{port})")
        from uvicorn import Config, Server

        from grimoire.mcp.app import create_mcp_app

        config = Config(
            app=create_mcp_app(),
            host=host,
            port=port,
            log_level="info",
        )
        server = Server(config)
        await server.serve()
```

The `mcp_server = create_mcp_server()` line inside this branch is deleted — the app factory builds the server. Leave the `if stdio:` branch untouched.

Then update the module docstring's usage line so it names the new endpoint:

```python
    grimoire mcp --sse --port 8100  # Authenticated SSE server at /mcp/sse
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_mcp_app.py -v`

Expected: 5 passed.

- [ ] **Step 6: Run the existing MCP suite for regressions**

Run: `uv run pytest tests/test_mcp.py tests/test_mcp_mlflow.py -v`

Expected: all pass. The REST-mounted `/mcp` path is unchanged, so the existing mount tests at `tests/test_mcp.py:1240-1269` must still pass.

- [ ] **Step 7: Lint and type check the changed files**

Run: `uv run ruff check grimoire/mcp/app.py grimoire/cli/mcp.py tests/test_mcp_app.py && uv run black --check grimoire/mcp/app.py grimoire/cli/mcp.py tests/test_mcp_app.py && uv run mypy grimoire/mcp/app.py grimoire/cli/mcp.py`

Expected: clean. If `black` reports reformatting, run it without `--check` and re-run.

- [ ] **Step 8: Commit**

```bash
git add grimoire/mcp/app.py grimoire/cli/mcp.py tests/test_mcp_app.py
git commit -m "Require API key on the standalone MCP SSE transport"
```

---

### Task 2: Build the container image

One multi-stage `Dockerfile`. The builder resolves dependencies into `/opt/venv` from `uv.lock`, then installs the project itself non-editable so the runtime stage needs no source copy.

**The torch problem, and why the build is shaped this way.** `uv.lock` pins `torch==2.11.0` from PyPI, which on Linux is the CUDA build and drags in 15 `nvidia-*` packages. On the host that is 1.2GB of torch plus 2.7GB of CUDA libraries. Installing that unconditionally yields a ~6GB image that is dead weight on any GPU-less deploy target. So the builder takes a `TORCH_VARIANT` argument: the default `cpu` variant removes the CUDA packages and reinstalls the same torch version from the PyTorch CPU index, while the `gpu` variant keeps what the lock resolved. Both variants install torch 2.11.0, so behavior matches across them.

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Test: `tests/deploy/test_dockerfile.py`

**Interfaces:**
- Consumes: `grimoire/mcp/app.py:app` from Task 1.
- Produces: an image whose default command serves the API on 8001; build args `TORCH_VARIANT` (default `cpu`) and `TORCH_VERSION` (default `2.11.0`), consumed by the GPU overlay in Task 4.

- [ ] **Step 1: Write the failing test**

Create `tests/deploy/test_dockerfile.py`:

```python
"""Dockerfile sanity tests.

These parse the Dockerfile as text rather than building it — a build takes
minutes and needs a daemon, while every failure these catch is textual: a
missing USER line, a secret baked into an ENV, a torch variant that silently
pulls three gigabytes of CUDA into an image nobody wanted it in.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


class TestFilesExist:
    def test_dockerfile_present(self) -> None:
        assert DOCKERFILE.is_file()

    def test_dockerignore_present(self) -> None:
        assert DOCKERIGNORE.is_file()


class TestDockerignore:
    def test_excludes_secrets_and_local_state(self) -> None:
        """Build context must not carry .env, local data, or the host venv."""
        entries = {
            line.strip()
            for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        for required in (".env", ".venv", "chroma_db/", "cache/", ".git"):
            assert required in entries, f"{required} missing from .dockerignore"


class TestRuntimeSecurity:
    def test_runs_as_non_root(self, dockerfile_text: str) -> None:
        """The last USER instruction must not be root."""
        users = re.findall(r"^USER\s+(\S+)", dockerfile_text, re.MULTILINE)
        assert users, "Dockerfile never drops privileges with USER"
        assert users[-1] != "root"

    def test_no_secrets_in_build_args_or_env(self, dockerfile_text: str) -> None:
        """No API key, password, or token is baked into the image.

        Scans every ``NAME=`` assignment rather than only the first token of
        each ENV line, because the ENV blocks use backslash continuations and
        a line-anchored pattern would silently skip everything after the first
        variable.
        """
        declarations = set(
            re.findall(r"\b([A-Z][A-Z0-9_]*)=", dockerfile_text)
        )
        forbidden = ("API_KEY", "PASSWORD", "SECRET", "TOKEN", "CREDENTIAL")
        leaked = sorted(
            name for name in declarations if any(f in name for f in forbidden)
        )
        assert not leaked, f"secret-bearing build variables: {leaked}"


class TestTorchVariant:
    def test_cpu_is_the_default_variant(self, dockerfile_text: str) -> None:
        assert re.search(r"^ARG\s+TORCH_VARIANT=cpu", dockerfile_text, re.MULTILINE)

    def test_torch_version_matches_the_lockfile(self, dockerfile_text: str) -> None:
        """Both image variants must install the version uv.lock pins."""
        lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
        locked = re.search(
            r'^name = "torch"\nversion = "([^"]+)"', lock, re.MULTILINE
        )
        assert locked, "torch not found in uv.lock"
        declared = re.search(
            r"^ARG\s+TORCH_VERSION=(\S+)", dockerfile_text, re.MULTILINE
        )
        assert declared, "Dockerfile does not declare TORCH_VERSION"
        assert declared.group(1) == locked.group(1)

    def test_cpu_variant_uses_the_pytorch_cpu_index(self, dockerfile_text: str) -> None:
        assert "download.pytorch.org/whl/cpu" in dockerfile_text


class TestDependencyResolution:
    def test_install_is_frozen(self, dockerfile_text: str) -> None:
        """Builds resolve from uv.lock, never from a fresh resolution."""
        assert "--frozen" in dockerfile_text


class TestHadolint:
    def test_hadolint_clean(self) -> None:
        if shutil.which("hadolint") is None:
            pytest.skip("hadolint not installed")
        result = subprocess.run(
            ["hadolint", str(DOCKERFILE)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/deploy/test_dockerfile.py -v`

Expected: FAIL — `test_dockerfile_present` and `test_dockerignore_present` fail, and the fixture raises `FileNotFoundError` for the rest.

- [ ] **Step 3: Write the .dockerignore**

Create `.dockerignore`:

```
# Secrets — never enter the build context
.env
.env.*
!.env.example

# Host virtualenv and caches
.venv
__pycache__/
*.pyc
.mypy_cache
.ruff_cache
.pytest_cache
.coverage

# Local runtime state — containers use volumes for these
chroma_db/
cache/
logs/
documents/

# VCS and tooling
.git
.github
.claude
.entire
.remember
.vscode
.qwen

# Docs and tests are not needed at runtime
docs/
tests/
*.md
!README.md
```

- [ ] **Step 4: Write the Dockerfile**

Create `Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Builder — resolve dependencies into /opt/venv from uv.lock
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

# Pinned to the host's uv version. uv.lock is revision 3; an older uv cannot
# read it, and an unpinned :latest makes builds non-reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# build-essential covers any dependency without a manylinux wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Dependencies first, in their own layer, so source edits do not re-resolve.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# uv.lock pins torch from PyPI, which on Linux is the CUDA build plus ~2.7GB
# of nvidia-* packages.  The default cpu variant swaps in the same torch
# version from the CPU index and drops the CUDA libraries; the gpu variant
# keeps what the lock resolved.  Both end up on the same torch version.
#
# --reinstall-package torch is essential: the CUDA build is already installed
# at this exact version, so without it uv treats the install as satisfied and
# leaves a torch binary linked against CUDA libraries that were just removed.
ARG TORCH_VARIANT=cpu
ARG TORCH_VERSION=2.11.0
RUN if [ "$TORCH_VARIANT" = "cpu" ]; then \
        nvidia_pkgs="$(uv pip list --python /opt/venv --format=freeze \
            | grep '^nvidia-' | cut -d= -f1 | tr '\n' ' ')"; \
        if [ -n "$nvidia_pkgs" ]; then \
            uv pip uninstall --python /opt/venv $nvidia_pkgs; \
        fi; \
        uv pip install --python /opt/venv \
            --index-url https://download.pytorch.org/whl/cpu \
            --reinstall-package torch \
            "torch==${TORCH_VERSION}"; \
    fi

# Install the project itself, non-editable, so the runtime stage needs no
# source tree on disk.
COPY README.md ./
COPY grimoire ./grimoire
RUN uv pip install --python /opt/venv --no-deps .

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# libmagic1 backs python-magic; libgl1 and libglib2.0-0 back Docling's PDF
# and image parsing, which grimoire.yaml enables by default; curl serves the
# container healthchecks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmagic1 \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 grimoire

COPY --from=builder --chown=grimoire:grimoire /opt/venv /opt/venv

WORKDIR /app

# Alembic is not part of the installed package but db-migrate needs it.
COPY --chown=grimoire:grimoire alembic ./alembic
COPY --chown=grimoire:grimoire alembic.ini ./alembic.ini

# HF_HOME and XDG_CACHE_HOME sit under the home directory so a single volume
# persists every model download across restarts.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/home/grimoire/.cache/huggingface \
    XDG_CACHE_HOME=/home/grimoire/.cache \
    GRIMOIRE_CONFIG=/app/grimoire.yaml

RUN mkdir -p /app/logs /app/cache && chown -R grimoire:grimoire /app

USER grimoire

EXPOSE 8001 8100

CMD ["uvicorn", "grimoire.api.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/deploy/test_dockerfile.py -v`

Expected: all pass, with `test_hadolint_clean` skipped unless `hadolint` is installed.

- [ ] **Step 6: Build the image and verify it actually works**

Run:

```bash
docker build -t grimoire:latest .
docker image ls grimoire:latest
```

Expected: build succeeds. Record the reported size — it should land near 2.5-3GB, not 6GB. If it exceeds 5GB, the CUDA removal did not take; inspect with `docker run --rm grimoire:latest uv pip list --python /opt/venv | grep nvidia` and fix the builder step before continuing.

Then smoke-test the installed package:

```bash
docker run --rm grimoire:latest python -c "import torch, grimoire; print(torch.__version__, torch.cuda.is_available())"
docker run --rm grimoire:latest grimoire --version
docker run --rm --user root grimoire:latest id -u   # sanity: image supports override
docker run --rm grimoire:latest id -u                # must print 10001, not 0
```

Expected: `2.11.0 False`, the Grimoire version string, then `0` and `10001`.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile .dockerignore tests/deploy/test_dockerfile.py
git commit -m "Add container image for the Grimoire application"
```

---

### Task 3: Define the application services in Compose

Adds `db-migrate`, `api`, `mcp`, and `watcher` beside the existing infrastructure services, and makes the `chromadb` service live for the first time by pointing the app at it over HTTP.

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Test: `tests/deploy/test_app_services.py`

**Interfaces:**
- Consumes: the image from Task 2; `grimoire.mcp.app:app` from Task 1.
- Produces: service names `db-migrate`, `api`, `mcp`, `watcher`; volumes `model_cache`, `app_logs`, `app_cache`; the YAML anchors `x-grimoire-env` and `x-grimoire-app`, which Tasks 4 and 5 override.

- [ ] **Step 1: Write the failing test**

Create `tests/deploy/test_app_services.py`:

```python
"""Compose application-service tests.

Parsing rather than running: every failure below produces a container that
starts cleanly and then cannot reach anything, which is slow and confusing
to diagnose live but trivial to catch in the YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"

APP_SERVICES = ("api", "mcp", "watcher")
LONG_RUNNING = APP_SERVICES


@pytest.fixture(scope="module")
def compose() -> dict:
    with BASE_COMPOSE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def services(compose: dict) -> dict:
    return compose["services"]


class TestServicesExist:
    @pytest.mark.parametrize("name", ("db-migrate", *APP_SERVICES))
    def test_service_defined(self, services: dict, name: str) -> None:
        assert name in services

    def test_migration_service_is_not_named_migrate(self, services: dict) -> None:
        """`grimoire migrate` means something else entirely — a vector-store stub."""
        assert "migrate" not in services


class TestStartupOrdering:
    def test_db_migrate_does_not_restart(self, services: dict) -> None:
        """A one-shot job that restarts would re-run Alembic in a loop."""
        assert str(services["db-migrate"]["restart"]) == "no"

    def test_db_migrate_runs_alembic(self, services: dict) -> None:
        command = services["db-migrate"]["command"]
        assert "alembic" in command
        assert "upgrade" in command
        assert "head" in command

    def test_db_migrate_waits_for_postgres(self, services: dict) -> None:
        depends = services["db-migrate"]["depends_on"]
        assert depends["postgres"]["condition"] == "service_healthy"

    @pytest.mark.parametrize("name", LONG_RUNNING)
    def test_app_waits_for_migrations_to_complete(
        self, services: dict, name: str
    ) -> None:
        """No app container may open a connection before the schema is current."""
        depends = services[name]["depends_on"]
        assert depends["db-migrate"]["condition"] == "service_completed_successfully"


class TestEnvironmentWiring:
    @pytest.fixture(scope="class")
    def app_env(self, services: dict) -> dict:
        return services["api"]["environment"]

    def test_database_points_at_the_postgres_service(self, app_env: dict) -> None:
        assert "@postgres:5432/" in app_env["GRIMOIRE_DATABASE__URL"]

    def test_chroma_uses_the_service_over_http(self, app_env: dict) -> None:
        """Setting host/port selects the HttpClient branch in the vector store."""
        assert app_env["GRIMOIRE_VECTOR_STORE__HOST"] == "chromadb"
        assert str(app_env["GRIMOIRE_VECTOR_STORE__PORT"]) == "8000"

    def test_redis_points_at_the_redis_service(self, app_env: dict) -> None:
        assert app_env["GRIMOIRE_REDIS__HOST"] == "redis"

    def test_config_path_is_absolute(self, app_env: dict) -> None:
        """GRIMOIRE_CONFIG defaults to a relative path; containers pin it."""
        assert app_env["GRIMOIRE_CONFIG"].startswith("/")

    @pytest.mark.parametrize("name", ("db-migrate", *APP_SERVICES))
    def test_no_service_points_at_localhost(self, services: dict, name: str) -> None:
        """localhost inside a container is the container, not the host."""
        env = services[name].get("environment", {})
        offenders = {
            key: value
            for key, value in env.items()
            # host.docker.internal is the deliberate route to the host's Ollama.
            if isinstance(value, str)
            and ("localhost" in value or "127.0.0.1" in value)
        }
        assert not offenders, f"{name} still points at localhost: {offenders}"


class TestOllamaWiring:
    @pytest.fixture(scope="class")
    def app_env(self, services: dict) -> dict:
        return services["api"]["environment"]

    def test_llm_url_reaches_the_host(self, app_env: dict) -> None:
        assert "host.docker.internal" in app_env["GRIMOIRE_LLM__URL"]

    def test_llm_url_has_no_v1_suffix(self, app_env: dict) -> None:
        """The agents append /api/generate; a /v1 base yields a 404."""
        assert "/v1" not in app_env["GRIMOIRE_LLM__URL"]

    @pytest.mark.parametrize("name", ("db-migrate", *APP_SERVICES))
    def test_host_gateway_mapping_present(self, services: dict, name: str) -> None:
        extra_hosts = services[name].get("extra_hosts", [])
        assert any("host-gateway" in entry for entry in extra_hosts)


class TestCommands:
    def test_api_serves_the_rest_app(self, services: dict) -> None:
        assert "grimoire.api.main:app" in services["api"]["command"]

    def test_mcp_serves_the_authenticated_app(self, services: dict) -> None:
        """Never the raw SSE app — that transport has no authentication."""
        assert "grimoire.mcp.app:app" in services["mcp"]["command"]

    def test_watcher_runs_the_watch_daemon(self, services: dict) -> None:
        command = services["watcher"]["command"]
        assert "watch" in command
        assert "start" in command


class TestVolumes:
    def test_model_cache_volume_declared(self, compose: dict) -> None:
        """Docling and sentence-transformers download weights on first use."""
        assert "model_cache" in compose["volumes"]

    @pytest.mark.parametrize("name", APP_SERVICES)
    def test_model_cache_mounted(self, services: dict, name: str) -> None:
        mounts = services[name]["volumes"]
        assert any(str(m).startswith("model_cache:") for m in mounts)

    def test_watcher_mounts_its_corpus_read_only(self, services: dict) -> None:
        mounts = [str(m) for m in services["watcher"]["volumes"]]
        watch_mounts = [m for m in mounts if "/data/watch" in m]
        assert watch_mounts, "watcher has no corpus mount"
        assert all(m.endswith(":ro") for m in watch_mounts)


class TestHealthchecks:
    @pytest.mark.parametrize("name", ("api", "mcp"))
    def test_http_services_have_healthchecks(self, services: dict, name: str) -> None:
        assert "healthcheck" in services[name]
        test = " ".join(services[name]["healthcheck"]["test"])
        assert "/health" in test
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/deploy/test_app_services.py -v`

Expected: FAIL — every `test_service_defined` case fails with `KeyError`, since no application services exist yet.

- [ ] **Step 3: Add the shared anchors to docker-compose.yml**

Insert this block at the top of `docker-compose.yml`, above the existing `services:` key:

```yaml
# Shared configuration for every container that runs the Grimoire image.
# Environment variables override grimoire.yaml — settings.py orders env above
# YAML — so the host's localhost defaults stay correct for bare-metal use
# while containers talk to service DNS names.
x-grimoire-env: &grimoire-env
  GRIMOIRE_CONFIG: /app/grimoire.yaml
  GRIMOIRE_DATABASE__URL: postgresql+asyncpg://${POSTGRES_USER:-grimoire}:${POSTGRES_PASSWORD:-grimoire}@postgres:5432/${POSTGRES_DB:-grimoire}
  GRIMOIRE_VECTOR_STORE__HOST: chromadb
  GRIMOIRE_VECTOR_STORE__PORT: "8000"
  GRIMOIRE_REDIS__HOST: redis
  GRIMOIRE_REDIS__PORT: "6379"
  GRIMOIRE_CELERY__BROKER_URL: redis://redis:6379/0
  GRIMOIRE_CELERY__RESULT_BACKEND: redis://redis:6379/1
  GRIMOIRE_LOGGING__LOG_DIR: /app/logs
  GRIMOIRE_CACHE__PATH: /app/cache
  # Ollama stays on the host by default.  Set GRIMOIRE_OLLAMA_URL to retarget
  # a remote or containerized daemon.  No /v1 suffix — the agents append
  # /api/generate themselves.
  GRIMOIRE_LLM__URL: ${GRIMOIRE_OLLAMA_URL:-http://host.docker.internal:11434}

x-grimoire-app: &grimoire-app
  image: grimoire:latest
  build:
    context: .
    dockerfile: Dockerfile
  environment: *grimoire-env
  env_file:
    - path: .env
      required: false
  extra_hosts:
    - "host.docker.internal:host-gateway"
  volumes:
    - ./grimoire.yaml:/app/grimoire.yaml:ro
    - model_cache:/home/grimoire
    - app_logs:/app/logs
    - app_cache:/app/cache
  networks:
    - grimoire-network
```

Two details that matter. `environment:` wins over `env_file:` in Compose, so a stale `GRIMOIRE_DATABASE__URL` in a developer's `.env` cannot break a container. And the Ollama override uses a distinct variable name, `GRIMOIRE_OLLAMA_URL`, precisely so that the `GRIMOIRE_LLM__URL=http://localhost:11434` already sitting in `.env` cannot be substituted back in.

- [ ] **Step 4: Add the four application services**

Append to the `services:` block in `docker-compose.yml`, after `chromadb` and before the `pgadmin` tools profile:

```yaml
  # One-shot Alembic migration.  Gates every application container so the
  # schema is current before anything connects, and so no two containers
  # race to migrate.  Named db-migrate because `grimoire migrate` is an
  # unrelated vector-store command.
  db-migrate:
    <<: *grimoire-app
    container_name: grimoire-db-migrate
    restart: "no"
    command: ["alembic", "upgrade", "head"]
    depends_on:
      postgres:
        condition: service_healthy

  # REST API.  Also serves authenticated MCP at /mcp for clients that would
  # rather not use the dedicated service.
  api:
    <<: *grimoire-app
    container_name: grimoire-api
    restart: unless-stopped
    command:
      ["uvicorn", "grimoire.api.main:app", "--host", "0.0.0.0", "--port", "8001"]
    ports:
      - "${GRIMOIRE_API_PORT:-8001}:8001"
    depends_on:
      db-migrate:
        condition: service_completed_successfully
      chromadb:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8001/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s

  # Dedicated MCP server, restartable independently of the REST API.
  mcp:
    <<: *grimoire-app
    container_name: grimoire-mcp
    restart: unless-stopped
    command:
      ["uvicorn", "grimoire.mcp.app:app", "--host", "0.0.0.0", "--port", "8100"]
    ports:
      - "${GRIMOIRE_MCP_PORT:-8100}:8100"
    depends_on:
      db-migrate:
        condition: service_completed_successfully
      chromadb:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8100/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s

  # Filesystem watcher.  The corpus mounts read-only: the watcher ingests
  # from it and never needs to write back.
  watcher:
    <<: *grimoire-app
    container_name: grimoire-watcher
    restart: unless-stopped
    command: ["grimoire", "watch", "start", "/data/watch"]
    volumes:
      - ./grimoire.yaml:/app/grimoire.yaml:ro
      - model_cache:/home/grimoire
      - app_logs:/app/logs
      - app_cache:/app/cache
      - ${GRIMOIRE_WATCH_DIR:-./documents}:/data/watch:ro
    depends_on:
      db-migrate:
        condition: service_completed_successfully
      chromadb:
        condition: service_healthy
      redis:
        condition: service_healthy
```

The watcher restates every volume because a YAML merge key replaces list values rather than appending to them.

- [ ] **Step 5: Declare the new volumes**

In the `volumes:` block at the bottom of `docker-compose.yml`, add:

```yaml
  model_cache:
    driver: local
  app_logs:
    driver: local
  app_cache:
    driver: local
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/deploy/test_app_services.py -v`

Expected: all pass.

- [ ] **Step 7: Validate the Compose file against Docker itself**

Run: `docker compose config --quiet && echo OK`

Expected: `OK`. This catches anchor and interpolation mistakes the YAML parser accepts but Compose rejects.

- [ ] **Step 8: Document the container variables in .env.example**

Append to `.env.example`:

```bash
# =============================================================================
# CONTAINER DEPLOYMENT (docker compose)
# =============================================================================
# These are read by docker-compose.yml on the host, not inside containers.
# Containers receive their configuration from the compose `environment:` block,
# which overrides both grimoire.yaml and anything in this file.
#
# NOTE: a mounted .env is NOT read inside a container — settings.py resolves
# env_file relative to the installed package, which lives in the image's venv.

# Where containers reach Ollama.  Defaults to the host daemon via the docker
# host gateway.  Point this at a remote or containerized Ollama to retarget.
# Must NOT end in /v1 — the agents append /api/generate themselves, so a /v1
# base produces /v1/api/generate and a 404.
GRIMOIRE_OLLAMA_URL=http://host.docker.internal:11434

# Published host ports for the application services
GRIMOIRE_API_PORT=8001
GRIMOIRE_MCP_PORT=8100

# Host directory the watcher container ingests from (mounted read-only)
GRIMOIRE_WATCH_DIR=./documents
```

- [ ] **Step 9: Commit**

```bash
git add docker-compose.yml .env.example tests/deploy/test_app_services.py
git commit -m "Add api, mcp, watcher, and db-migrate services to compose"
```

---

### Task 4: Add the GPU overlay

The base image ships CPU-only torch, so GPU support is not merely a device reservation — the overlay must also build the CUDA image variant. It sets `TORCH_VARIANT=gpu`, tags the result separately so the two variants never overwrite each other, reserves the device, and tells the embedder to use it.

**Files:**
- Create: `docker-compose.gpu.yml`
- Test: `tests/deploy/test_gpu_overlay.py`

**Interfaces:**
- Consumes: build args `TORCH_VARIANT` and `TORCH_VERSION` from Task 2; service names from Task 3.
- Produces: an overlay usable as `docker compose -f docker-compose.yml -f docker-compose.gpu.yml`.

- [ ] **Step 1: Write the failing test**

Create `tests/deploy/test_gpu_overlay.py`:

```python
"""GPU overlay tests.

The overlay's whole job is to change three things — the torch variant, the
device reservation, and the embedding device.  An overlay that quietly also
changes a port or a command is the failure worth guarding against, so these
tests assert containment as much as content.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
GPU_OVERLAY = REPO_ROOT / "docker-compose.gpu.yml"

GPU_SERVICES = ("api", "mcp", "watcher")


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def base() -> dict:
    return _load(BASE_COMPOSE)


@pytest.fixture(scope="module")
def overlay() -> dict:
    return _load(GPU_OVERLAY)


def test_overlay_present() -> None:
    assert GPU_OVERLAY.is_file()


@pytest.mark.parametrize("name", GPU_SERVICES)
def test_builds_the_cuda_variant(overlay: dict, name: str) -> None:
    """A device reservation alone does nothing — CPU torch cannot use a GPU."""
    args = overlay["services"][name]["build"]["args"]
    assert args["TORCH_VARIANT"] == "gpu"


@pytest.mark.parametrize("name", GPU_SERVICES)
def test_uses_a_distinct_image_tag(overlay: dict, base: dict, name: str) -> None:
    """The GPU image must not overwrite the CPU image under the same tag."""
    assert overlay["services"][name]["image"] != base["services"][name]["image"]


@pytest.mark.parametrize("name", GPU_SERVICES)
def test_reserves_an_nvidia_device(overlay: dict, name: str) -> None:
    devices = overlay["services"][name]["deploy"]["resources"]["reservations"][
        "devices"
    ]
    assert any(d["driver"] == "nvidia" for d in devices)
    assert any("gpu" in d["capabilities"] for d in devices)


@pytest.mark.parametrize("name", GPU_SERVICES)
def test_selects_the_cuda_embedding_device(overlay: dict, name: str) -> None:
    assert overlay["services"][name]["environment"]["GRIMOIRE_EMBEDDINGS__DEVICE"] == "cuda"


@pytest.mark.parametrize("name", GPU_SERVICES)
def test_overlay_changes_nothing_else(overlay: dict, name: str) -> None:
    """Containment: no ports, volumes, commands, or dependencies touched."""
    allowed = {"image", "build", "deploy", "environment"}
    assert set(overlay["services"][name]) <= allowed


def test_overlay_does_not_touch_infrastructure(overlay: dict) -> None:
    assert set(overlay["services"]) <= set(GPU_SERVICES)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/deploy/test_gpu_overlay.py -v`

Expected: FAIL — `test_overlay_present` fails and the `overlay` fixture raises `FileNotFoundError`.

- [ ] **Step 3: Write the GPU overlay**

Create `docker-compose.gpu.yml`:

```yaml
# GPU overlay.
#
# The base image ships CPU-only torch to stay portable, so reserving a device
# is not enough on its own — this overlay rebuilds the image with the CUDA
# torch variant, tags it separately, reserves the GPU, and points the embedder
# at it.
#
# Usage:
#   docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
#
# Requires the NVIDIA Container Toolkit on the host.

services:
  api: &gpu-service
    image: grimoire:gpu
    build:
      context: .
      dockerfile: Dockerfile
      args:
        TORCH_VARIANT: gpu
        TORCH_VERSION: "2.11.0"
    environment:
      GRIMOIRE_EMBEDDINGS__DEVICE: cuda
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  mcp: *gpu-service

  watcher: *gpu-service
```

Note that Compose merges the overlay's `environment` into the base service's rather than replacing it, so `GRIMOIRE_EMBEDDINGS__DEVICE` is added while every base variable survives.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/deploy/test_gpu_overlay.py -v`

Expected: all pass.

- [ ] **Step 5: Validate the merged configuration**

Run: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml config --quiet && echo OK`

Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.gpu.yml tests/deploy/test_gpu_overlay.py
git commit -m "Add GPU compose overlay building the CUDA torch variant"
```

---

### Task 5: Add the development overlay and validate every overlay combination

**Files:**
- Create: `docker-compose.dev.yml`
- Test: `tests/deploy/test_dev_overlay.py`

**Interfaces:**
- Consumes: service names and anchors from Task 3.
- Produces: an overlay usable as `docker compose -f docker-compose.yml -f docker-compose.dev.yml`.

- [ ] **Step 1: Write the failing test**

Create `tests/deploy/test_dev_overlay.py`:

```python
"""Development overlay tests, plus a Compose validation sweep.

The sweep is the broadest guard in the deploy suite: it asks Docker itself
whether each overlay combination resolves, which catches anchor, merge, and
interpolation errors that a plain YAML parse accepts.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_OVERLAY = REPO_ROOT / "docker-compose.dev.yml"

DEV_SERVICES = ("api", "mcp")

COMBINATIONS = [
    ["docker-compose.yml"],
    ["docker-compose.yml", "docker-compose.gpu.yml"],
    ["docker-compose.yml", "docker-compose.dev.yml"],
    ["docker-compose.yml", "docker-compose.security.yml"],
]


@pytest.fixture(scope="module")
def overlay() -> dict:
    with DEV_OVERLAY.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_overlay_present() -> None:
    assert DEV_OVERLAY.is_file()


@pytest.mark.parametrize("name", DEV_SERVICES)
def test_source_is_bind_mounted(overlay: dict, name: str) -> None:
    """Edits on the host must reach the container without a rebuild."""
    mounts = [str(m) for m in overlay["services"][name]["volumes"]]
    assert any(m.startswith("./grimoire:") for m in mounts)


@pytest.mark.parametrize("name", DEV_SERVICES)
def test_reload_enabled(overlay: dict, name: str) -> None:
    assert "--reload" in overlay["services"][name]["command"]


def test_watcher_is_not_reloaded(overlay: dict) -> None:
    """The watcher holds long-lived filesystem handles; reloading breaks them."""
    assert "watcher" not in overlay["services"]


@pytest.mark.parametrize("files", COMBINATIONS, ids=lambda f: "+".join(f))
def test_compose_configuration_resolves(files: list[str]) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    args = ["docker", "compose"]
    for name in files:
        args += ["-f", name]
    args += ["config", "--quiet"]
    result = subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/deploy/test_dev_overlay.py -v`

Expected: FAIL — `test_overlay_present` fails, the overlay fixture raises `FileNotFoundError`, and the dev combination in the sweep fails because the file does not exist.

- [ ] **Step 3: Write the development overlay**

Create `docker-compose.dev.yml`:

```yaml
# Development overlay.
#
# Bind-mounts the source tree over the installed package and enables uvicorn's
# reloader, so edits take effect without rebuilding the image.
#
# Usage:
#   docker compose -f docker-compose.yml -f docker-compose.dev.yml up

services:
  api:
    volumes:
      - ./grimoire:/opt/venv/lib/python3.13/site-packages/grimoire:ro
      - ./grimoire.yaml:/app/grimoire.yaml:ro
      - model_cache:/home/grimoire
      - app_logs:/app/logs
      - app_cache:/app/cache
    command:
      - uvicorn
      - grimoire.api.main:app
      - --host
      - 0.0.0.0
      - --port
      - "8001"
      - --reload
      - --reload-dir
      - /opt/venv/lib/python3.13/site-packages/grimoire
    environment:
      GRIMOIRE_LOGGING__LEVEL: DEBUG

  mcp:
    volumes:
      - ./grimoire:/opt/venv/lib/python3.13/site-packages/grimoire:ro
      - ./grimoire.yaml:/app/grimoire.yaml:ro
      - model_cache:/home/grimoire
      - app_logs:/app/logs
      - app_cache:/app/cache
    command:
      - uvicorn
      - grimoire.mcp.app:app
      - --host
      - 0.0.0.0
      - --port
      - "8100"
      - --reload
      - --reload-dir
      - /opt/venv/lib/python3.13/site-packages/grimoire
    environment:
      GRIMOIRE_LOGGING__LEVEL: DEBUG
```

The package is installed into the image's site-packages, so the bind mount targets that path rather than `/app`. The watcher is deliberately absent: reloading it would drop the filesystem watches it exists to hold.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/deploy/test_dev_overlay.py -v`

Expected: all pass. If the site-packages path is wrong, the container will start but serve stale code — verify with `docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm api python -c "import grimoire; print(grimoire.__file__)"` and confirm the path matches the mount target.

- [ ] **Step 5: Run the whole deploy suite**

Run: `uv run pytest tests/deploy/ -v`

Expected: all pass, including the pre-existing `test_compose_overlay.py`.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.dev.yml tests/deploy/test_dev_overlay.py
git commit -m "Add development compose overlay with source bind mounts"
```

---

### Task 6: Document the containerized deployment

**Files:**
- Create: `docs/deploy/docker.md`
- Modify: `README.md` (the Quick Start section at line 18, and the MCP section at line 282)
- Modify: `.vscode/mcp.json`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: service names, ports, and environment variables from Tasks 2-5; the `/mcp/sse` endpoint path from Task 1.
- Produces: no code.

- [ ] **Step 1: Write the deployment guide**

Create `docs/deploy/docker.md` covering, in this order:

1. **Prerequisites** — Docker Engine with Compose v2, roughly 10GB of disk for the image and model cache, and Ollama reachable from the host.
2. **Quick start** — `cp .env.example .env`, edit credentials, `docker compose up -d --build`, then `curl http://localhost:8001/health`.
3. **Services table** — `postgres`, `redis`, `chromadb`, `db-migrate`, `api` (8001), `mcp` (8100), `watcher`, and the `tools`-profile services, each with its purpose and published port.
4. **Configuration** — the precedence chain (env beats `grimoire.yaml`, `settings.py:1107`); the full `GRIMOIRE_*` table from the compose anchor; and an explicit warning that a mounted `.env` is **not** read inside containers because `settings.py:1049` resolves it relative to the installed package.
5. **Ollama** — why `host.docker.internal` is the default, how `GRIMOIRE_OLLAMA_URL` retargets it, and that the URL must not end in `/v1`.
6. **Overlays** — the exact `docker compose -f ... -f ...` invocations for GPU and dev, with the note that the GPU overlay rebuilds the image as `grimoire:gpu`.
7. **Volumes and backup** — what each named volume holds and which ones matter for backup (`postgres_data`, `chromadb_data`; `model_cache` and `app_cache` are rebuildable).
8. **Troubleshooting** — at minimum these four, since they are the failures this stack actually produces:
   - *LLM generation fails with 404* — a `/v1` suffix on the Ollama URL, or the host daemon not listening on the docker bridge.
   - *Chroma connection refused* — the `chromadb` service unhealthy, or `GRIMOIRE_VECTOR_STORE__HOST` unset so the app silently fell back to the embedded client.
   - *App containers exit immediately at first start* — `db-migrate` failed; read its logs, since the others gate on its success.
   - *First ingestion is very slow* — a cold `model_cache`; Docling and sentence-transformers are downloading weights.

- [ ] **Step 2: Add a Docker section to the README**

In `README.md`, after the existing Quick Start block (which ends near line 96), add a "Run with Docker" subsection giving the three-command startup and linking to `docs/deploy/docker.md`. Keep the bare-metal instructions exactly as they are — both paths are supported.

- [ ] **Step 3: Update the README's MCP transport documentation**

In `README.md` at line 288, the SSE bullet currently reads that the API server mounts an endpoint at `/mcp`. Replace the transports list with:

```markdown
**Available transports:**
- **stdio** – run `grimoire mcp --stdio` and point your AI client at it (requires `GRIMOIRE_API_KEY` env var)
- **SSE** – `grimoire mcp --sse --port 8100`, or the `mcp` container, serves an authenticated endpoint at `/mcp/sse`. The REST API also mounts the same endpoint at `/mcp/sse` when you run `uvicorn grimoire.api.main:app`.

> **Changed in 2.1:** the standalone SSE transport previously served MCP at `/sse` with no authentication. It now requires the same `X-API-Key` header as every other transport, and the endpoint moved to `/mcp/sse`. Update any client that pointed at the old path.
```

- [ ] **Step 4: Register Grimoire's MCP server in .vscode/mcp.json**

The file currently lists only an unrelated `backboard-docs` server; Grimoire's own server has never been registered. Replace its contents with:

```json
{
	"servers": {
		"backboard-docs": {
			"type": "http",
			"url": "https://backboard-docs.docsalot.dev/api/mcp"
		},
		"grimoire": {
			"type": "sse",
			"url": "http://localhost:8100/mcp/sse",
			"headers": {
				"X-API-Key": "${input:grimoire-api-key}"
			}
		}
	},
	"inputs": [
		{
			"id": "grimoire-api-key",
			"type": "promptString",
			"description": "Grimoire API key (grimoire keys create)",
			"password": true
		}
	]
}
```

The key is a prompted input rather than a literal so no credential is committed.

- [ ] **Step 5: Update the changelog**

Add an entry to `CHANGELOG.md` following the file's existing format, covering:

- **Security:** the standalone MCP SSE transport now requires an API key. Previously `grimoire mcp --sse` served every tool unauthenticated, exposing the corpus to anyone who could reach the port.
- **Added:** container image, Compose services for the API, MCP server, watcher, and schema migration, plus GPU and development overlays.
- **Changed:** the standalone SSE endpoint moved from `/sse` to `/mcp/sse`; containers use the ChromaDB service over HTTP instead of an embedded store.

- [ ] **Step 6: Verify the documented commands actually run**

Run each command quoted in the quick-start section of `docs/deploy/docker.md` and confirm it behaves as written. Documentation that was never executed is the most common source of a broken onboarding.

- [ ] **Step 7: Commit**

```bash
git add docs/deploy/docker.md README.md .vscode/mcp.json CHANGELOG.md
git commit -m "Document containerized deployment and the MCP SSE endpoint change"
```

---

### Task 7: Verify the full stack end to end

The spec chose a fresh start over data migration specifically so this task exercises the containerized ingestion pipeline — parsing, chunking, embedding, tagging, and the dual write to Postgres and Chroma. That path is the most likely to break under containerization and the part a migration script would have skipped.

**Files:**
- Modify: `docs/deploy/docker.md` (troubleshooting additions, if this task uncovers new failure modes)

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: no code. A verification record.

- [ ] **Step 1: Bring up a clean stack**

Run:

```bash
docker compose down -v
docker compose up -d --build
docker compose ps
```

Expected: `db-migrate` shows `exited (0)`; `postgres`, `redis`, `chromadb`, `api`, `mcp`, and `watcher` all show running, with `api` and `mcp` healthy once their start period elapses. If `api` is stuck restarting, read `docker compose logs db-migrate` first — the others gate on it.

- [ ] **Step 2: Confirm the schema was created**

Run: `docker compose exec postgres psql -U grimoire -d grimoire -c '\dt'`

Expected: the Grimoire tables are listed. An empty result means Alembic ran against a different database than the app uses — compare `GRIMOIRE_DATABASE__URL` with the `postgres` service's credentials.

- [ ] **Step 3: Confirm the app uses the Chroma service, not an embedded store**

Run:

```bash
docker compose exec api python -c "import chromadb; c = chromadb.HttpClient(host='chromadb', port=8000); print(c.heartbeat())"
docker compose exec api sh -c 'ls -la /app/chroma_db 2>&1 | head -1'
```

Expected: a heartbeat value, and no `/app/chroma_db` directory. If that directory exists, the app fell back to `PersistentClient` and `GRIMOIRE_VECTOR_STORE__HOST` did not take effect.

- [ ] **Step 4: Create an API key and exercise the REST API**

Run:

```bash
docker compose exec api grimoire keys create --name container-test --tier agent
curl http://localhost:8001/health
curl -H "X-API-Key: <key>" http://localhost:8001/api/v1/documents
```

Expected: a key is printed once, `/health` returns `{"status":"ok"}`, and the documents endpoint returns an empty list rather than an error.

- [ ] **Step 5: Ingest a document and confirm the full pipeline**

Run:

```bash
docker compose cp documents/Mod1-studyguide.pdf api:/tmp/test.pdf
docker compose exec api grimoire ingest /tmp/test.pdf
```

Expected: ingestion completes. The first run is slow because the model cache is cold — Docling and sentence-transformers are downloading weights. Watch `docker compose logs -f api` for download progress rather than assuming a hang.

Then confirm the dual write landed:

```bash
docker compose exec postgres psql -U grimoire -d grimoire -c 'SELECT count(*) FROM documents;'
docker compose exec api python -c "import chromadb; c = chromadb.HttpClient(host='chromadb', port=8000); print(c.get_collection('documents').count())"
```

Expected: a non-zero count from both. A row in Postgres with zero vectors in Chroma means the embedding or vector-write step failed silently — check the API logs.

- [ ] **Step 6: Verify the model cache persists across a restart**

Run:

```bash
docker compose restart api
time docker compose exec api grimoire ingest /tmp/test.pdf
```

Expected: the second ingestion starts embedding almost immediately, with no model downloads. If weights download again, the `model_cache` volume is not covering the paths `HF_HOME` and `XDG_CACHE_HOME` point at.

- [ ] **Step 7: Query through the RAG pipeline to confirm Ollama is reachable**

Run: `docker compose exec api grimoire ask "what is in the study guide?"`

Expected: a generated answer. A 404 mentioning `/v1/api/generate` means a `/v1` suffix crept into the LLM URL. A connection error means the container cannot reach the host daemon — confirm Ollama listens on more than the loopback interface.

- [ ] **Step 8: Verify MCP authentication over the network**

Run:

```bash
curl -i http://localhost:8100/health
curl -i http://localhost:8100/mcp/sse
curl -i -H "X-API-Key: <key>" http://localhost:8100/mcp/sse --max-time 3
```

Expected: `200` on health, **`401` on the unauthenticated SSE request**, and a non-401 on the authenticated one. The middle result is the whole point of Task 1 — if it returns anything other than 401, stop and fix before going further.

- [ ] **Step 9: Verify the watcher ingests a new file**

Run:

```bash
cp documents/Mod2-studyguide.pdf ./documents/watch-test.pdf
sleep 60
docker compose logs watcher | tail -20
```

Expected: the watcher logs detection and ingestion. If nothing happens, inotify events are not propagating across the bind mount — a known WSL2 weakness. In that case, document the `--poll-interval` workaround in the troubleshooting section rather than leaving the behavior unexplained.

- [ ] **Step 10: Run the complete test suite**

Run: `uv run pytest -v`

Expected: all pass, with no regression from Task 1's changes to the MCP transport.

- [ ] **Step 11: Run the full quality gate**

Run: `uv run pre-commit run --all-files`

Expected: ruff, black, mypy, and bandit all clean.

- [ ] **Step 12: Commit any troubleshooting additions**

```bash
git add docs/deploy/docker.md
git commit -m "Document container failure modes found during stack verification"
```

Skip this commit if Step 9 and the rest surfaced nothing new to record.

---

## Notes for the Executor

**Task 1 is a security fix and must land first.** Do not reorder it behind the Dockerfile. Publishing an MCP port before that fix exposes the corpus.

**The torch swap in Task 2 is the riskiest step.** If removing the `nvidia-*` packages and reinstalling CPU torch proves unreliable across a `uv` version change, the fallback is to accept the CUDA image the lock resolves — roughly 6GB — and document the size. Do not respond by unpinning torch or dropping `--frozen`; a reproducible large image beats an irreproducible small one.

**Compose merge semantics differ by type.** Mappings such as `environment` merge key by key, while sequences such as `volumes`, `ports`, and `command` are replaced wholesale. This is why the watcher service restates every volume and why the dev overlay restates full command lists.

**When a compose test and Docker disagree, Docker is right.** `docker compose config` resolves anchors, merges, and variable interpolation that a plain `yaml.safe_load` does not. The parsing tests are fast guards, not the source of truth.
