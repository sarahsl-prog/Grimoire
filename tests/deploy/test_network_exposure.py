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


# Services allowed to publish on every interface. Only the TLS reverse proxy
# should ever be here; everything else is reached through it or over SSH.
PUBLIC_SERVICES: frozenset[str] = frozenset()


def _published_ports(path: Path) -> list[tuple[str, str]]:
    """Return ``(service, port spec)`` for every published port in a file."""
    out: list[tuple[str, str]] = []
    for name, service in (_load(path).get("services") or {}).items():
        for port in service.get("ports") or []:
            if isinstance(port, dict):
                # Long syntax: host_ip is the bind address.
                spec = f"{port.get('host_ip', '')}:{port.get('published')}"
            else:
                spec = str(port)
            out.append((name, spec))
    return out


class TestLoopbackOnly:
    @pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
    def test_non_proxy_ports_bind_to_loopback(self, path: Path) -> None:
        for name, spec in _published_ports(path):
            if name in PUBLIC_SERVICES:
                continue
            assert spec.startswith("127.0.0.1:"), (
                f"{path.name}:{name} publishes {spec!r} on every interface; "
                "on a VPS that is reachable from the internet (Docker "
                "bypasses UFW). Bind it to 127.0.0.1."
            )

    @pytest.mark.parametrize(
        "name", ["api", "mcp", "pgadmin", "redis-commander", "chromadb"]
    )
    def test_sensitive_service_has_a_loopback_port(self, name: str) -> None:
        """Guard the specific services audit V-1 found exposed."""
        ports = _load(REPO_ROOT / "docker-compose.yml")["services"][name]["ports"]
        assert ports and all(str(p).startswith("127.0.0.1:") for p in ports)
