"""Network-exposure tests for the compose stack.

Grimoire is deployed on a public VPS (Hetzner). Two facts drive these tests:

* Docker writes its own iptables NAT rules ahead of UFW's, so ``ufw deny``
  does not protect a published container port. How a port is published in
  the compose file *is* the firewall.
* ChromaDB 1.x (the Rust server) dropped built-in authentication, so the
  only access control for it is network isolation.

Like the other deploy tests, these parse YAML rather than run Docker.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yml"))
ENV_EXAMPLES = [REPO_ROOT / ".env.example", REPO_ROOT / ".env.security.example"]


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _env_entries(service: dict) -> list[str]:
    """Return a service's environment as ``KEY=value`` strings.

    Compose accepts either a list of ``KEY=value`` or a mapping; normalise so
    assertions don't care which style a file uses.
    """
    env = service.get("environment") or []
    if isinstance(env, dict):
        return [f"{k}={v}" for k, v in env.items()]
    return [str(e) for e in env]


class TestChromaAuth:
    """No configuration may imply ChromaDB enforces a token: it doesn't."""

    @pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
    def test_compose_sets_no_chroma_auth_env(self, path: Path) -> None:
        for name, service in (_load(path).get("services") or {}).items():
            for entry in _env_entries(service):
                assert not entry.startswith("CHROMA_SERVER_AUTH"), (
                    f"{path.name}:{name} sets {entry.split('=')[0]}, which "
                    "Chroma 1.x ignores"
                )

    @pytest.mark.parametrize("path", ENV_EXAMPLES, ids=lambda p: p.name)
    def test_env_examples_have_no_chroma_token(self, path: Path) -> None:
        for line in path.read_text(encoding="utf-8").splitlines():
            assert not line.strip().startswith("CHROMA_SERVER_AUTH"), line
