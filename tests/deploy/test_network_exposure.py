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

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yml"))
CADDYFILE = REPO_ROOT / "deploy" / "caddy" / "Caddyfile"
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
PUBLIC_SERVICES: frozenset[str] = frozenset({"caddy"})


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


class TestCaddyProxy:
    """The TLS proxy is the single public entry point."""

    @pytest.fixture(scope="class")
    def caddy(self) -> dict:
        return _load(REPO_ROOT / "docker-compose.yml")["services"]["caddy"]

    def test_is_opt_in_via_profile(self, caddy: dict) -> None:
        """A plain `docker compose up` on a laptop must not grab 80/443."""
        assert caddy["profiles"] == ["proxy"]

    def test_publishes_only_http_and_https(self, caddy: dict) -> None:
        assert sorted(caddy["ports"]) == ["443:443", "443:443/udp", "80:80"]

    def test_image_is_pinned_to_a_patch_version(self, caddy: dict) -> None:
        assert re.fullmatch(r"caddy:\d+\.\d+\.\d+-alpine", caddy["image"])

    def test_mounts_caddyfile_read_only(self, caddy: dict) -> None:
        assert "./deploy/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" in caddy["volumes"]

    def test_persists_certificates(self, caddy: dict) -> None:
        """Losing /data means re-issuing certs and hitting ACME rate limits."""
        assert any(str(v).startswith("caddy_data:/data") for v in caddy["volumes"])
        top = _load(REPO_ROOT / "docker-compose.yml")["volumes"]
        assert "caddy_data" in top and "caddy_config" in top

    def test_refuses_to_start_without_domain(self, caddy: dict) -> None:
        """No ${VAR:?}: that breaks every compose command. Check at start."""
        script = caddy["command"][0]
        assert "GRIMOIRE_DOMAIN" in script and "exit 1" in script
        assert "${GRIMOIRE_DOMAIN:?" not in str(caddy["environment"])

    @pytest.mark.skipif(shutil.which("sh") is None, reason="sh not installed")
    @pytest.mark.parametrize(
        ("env", "expected_rc"),
        [({}, 1), ({"GRIMOIRE_DOMAIN": "g.example.com"}, 1)],
    )
    def test_start_guard_exits_when_unset(
        self, caddy: dict, env: dict[str, str], expected_rc: int
    ) -> None:
        # Compose turns $$ into $; replicate that, and stop before exec caddy.
        script = caddy["command"][0].replace("$$", "$")
        guard = script.split("exec caddy")[0] + "exit 0"
        sh = shutil.which("sh")
        assert sh is not None
        result = subprocess.run(  # noqa: S603 — sh path resolved via shutil.which
            [sh, "-c", guard], env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == expected_rc, result.stderr

    def test_waits_for_healthy_upstreams(self, caddy: dict) -> None:
        for name in ("api", "mcp"):
            assert caddy["depends_on"][name]["condition"] == "service_healthy"


class TestCaddyfile:
    @pytest.fixture(scope="class")
    def text(self) -> str:
        return CADDYFILE.read_text(encoding="utf-8")

    def test_site_address_comes_from_env(self, text: str) -> None:
        assert re.search(r"^\{\$GRIMOIRE_DOMAIN\} \{", text, re.MULTILINE)

    def test_upstreams_use_service_dns_names(self, text: str) -> None:
        """Host ports are loopback-only, so Caddy must use grimoire-network."""
        assert "reverse_proxy api:8001" in text
        assert "reverse_proxy mcp:8100" in text
        assert "localhost:8001" not in text and "127.0.0.1:8001" not in text

    def test_mcp_path_is_not_stripped(self, text: str) -> None:
        """Both apps mount MCP at /mcp; stripping would break SSE endpoints."""
        assert "handle /mcp/*" in text
        assert "handle_path" not in text and "uri strip_prefix" not in text

    def test_sse_is_not_buffered(self, text: str) -> None:
        assert "flush_interval -1" in text

    def test_sends_hsts(self, text: str) -> None:
        assert "Strict-Transport-Security" in text

    def test_braces_balance(self, text: str) -> None:
        """Cheap structural check; `caddy validate` is the real one."""
        code = "\n".join(line.split("#")[0] for line in text.splitlines())
        assert code.count("{") == code.count("}")


class TestProxyHeaders:
    """uvicorn must trust Caddy's X-Forwarded-For or rate limits collapse."""

    def test_app_env_trusts_docker_private_range(self) -> None:
        compose = _load(REPO_ROOT / "docker-compose.yml")
        for name in ("api", "mcp"):
            env = compose["services"][name]["environment"]
            assert env["FORWARDED_ALLOW_IPS"] == (
                "${GRIMOIRE_TRUSTED_PROXIES:-172.16.0.0/12}"
            )

    @pytest.mark.parametrize(
        ("overlay", "subnet"),
        [
            ("docker-compose.yml", "172.28.0.0/16"),
            ("docker-compose.security.yml", "172.29.0.0/16"),
        ],
    )
    def test_default_trust_range_covers_network_subnet(
        self, overlay: str, subnet: str
    ) -> None:
        import ipaddress

        config = _load(REPO_ROOT / overlay)["networks"]["grimoire-network"]
        assert config["ipam"]["config"][0]["subnet"] == subnet
        assert ipaddress.ip_network(subnet).subnet_of(
            ipaddress.ip_network("172.16.0.0/12")
        )
