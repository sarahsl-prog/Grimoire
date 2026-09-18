"""Development overlay tests, plus a Compose validation sweep.

The sweep is the broadest guard in the deploy suite: it asks Docker itself
whether each overlay combination resolves, which catches anchor, merge, and
interpolation errors that a plain YAML parse accepts.
"""

from __future__ import annotations

import json
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


def test_resolved_ollama_url_reaches_the_host() -> None:
    """Guard against a developer's .env clobbering the Ollama endpoint.

    `test_app_services.py::TestOllamaWiring` reads the raw compose YAML with
    `yaml.safe_load`, which never performs `${VAR:-default}` interpolation —
    so those tests only ever see the literal default text and pass no matter
    what a real environment resolves to. That can't catch the actual risk: a
    developer's `.env` setting `GRIMOIRE_OLLAMA_URL` (directly, or via a
    stray `GRIMOIRE_LLM__URL`) to something like `http://localhost:11434`,
    which is meaningless inside a container and would silently break the LLM
    calls. Asking `docker compose config` for the resolved value exercises
    the same interpolation Compose performs for a real `up`, against
    whatever `.env` actually sits in the repo right now.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    args = [
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "config",
        "--format",
        "json",
    ]
    result = subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    config = json.loads(result.stdout)
    resolved_url = config["services"]["api"]["environment"]["GRIMOIRE_LLM__URL"]
    assert "host.docker.internal" in resolved_url, resolved_url
    assert "/v1" not in resolved_url, resolved_url
