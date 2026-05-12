"""Phase 10 — docker-compose security overlay sanity tests.

These don't run docker — they just parse both compose files and assert
the overlay flips the bits we care about (security domain env, isolated
ports / volumes, security-corpus mount). Plus a bash ``-n`` syntax check
on ``scripts/security/seed_corpus.sh``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
SECURITY_OVERLAY = REPO_ROOT / "docker-compose.security.yml"
SEED_SCRIPT = REPO_ROOT / "scripts" / "security" / "seed_corpus.sh"
ENV_SECURITY_EXAMPLE = REPO_ROOT / ".env.security.example"


def _load(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Replicate Docker Compose's per-key override semantics for tests.

    Docker Compose 'merges' two files by deep-merging dicts and *replacing*
    lists (ports, volumes, command, etc.). We don't try to fully replicate
    that — for the assertions in this file we only need the overlay's
    explicit overrides to win, which a recursive dict merge already gives us.
    """
    out: dict = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Compose files
# ---------------------------------------------------------------------------


class TestComposeFilesExist:
    def test_base_compose_present(self) -> None:
        assert BASE_COMPOSE.is_file()

    def test_security_overlay_present(self) -> None:
        assert SECURITY_OVERLAY.is_file()

    def test_security_env_example_present(self) -> None:
        assert ENV_SECURITY_EXAMPLE.is_file()


class TestComposeOverlay:
    """The overlay must isolate the data plane and flip security mode."""

    @pytest.fixture
    def merged(self) -> dict:
        return _deep_merge(_load(BASE_COMPOSE), _load(SECURITY_OVERLAY))

    def test_overlay_parses_as_yaml(self) -> None:
        assert isinstance(_load(SECURITY_OVERLAY), dict)

    def test_postgres_isolated_port(self, merged: dict) -> None:
        ports = merged["services"]["postgres"]["ports"]
        assert any("5433" in p for p in ports), ports

    def test_postgres_security_domain_env(self, merged: dict) -> None:
        env = merged["services"]["postgres"]["environment"]
        assert env.get("GRIMOIRE_SECURITY__DOMAIN") == "security"

    def test_postgres_security_db_default(self, merged: dict) -> None:
        env = merged["services"]["postgres"]["environment"]
        # The default must point at the security DB so a fresh deploy with
        # an unset POSTGRES_DB doesn't accidentally land in the general one.
        assert "grimoire_security" in env["POSTGRES_DB"]

    def test_postgres_isolated_volume(self, merged: dict) -> None:
        vols = merged["services"]["postgres"]["volumes"]
        joined = " ".join(vols)
        assert "postgres_data_security" in joined, vols

    def test_chromadb_isolated_port(self, merged: dict) -> None:
        ports = merged["services"]["chromadb"]["ports"]
        assert any("8002" in p for p in ports), ports

    def test_chromadb_mounts_security_corpus(self, merged: dict) -> None:
        vols = merged["services"]["chromadb"]["volumes"]
        joined = " ".join(vols)
        assert "/security-corpus" in joined, vols

    def test_chromadb_isolated_volume(self, merged: dict) -> None:
        vols = merged["services"]["chromadb"]["volumes"]
        joined = " ".join(vols)
        assert "chromadb_data_security" in joined, vols

    def test_redis_isolated_volume(self, merged: dict) -> None:
        vols = merged["services"]["redis"]["volumes"]
        joined = " ".join(vols)
        assert "redis_data_security" in joined, vols

    def test_no_ollama_container(self, merged: dict) -> None:
        """Security profile is cloud-LLM only — no local Ollama container."""
        assert "ollama" not in merged["services"]

    def test_overlay_declares_security_volumes(self) -> None:
        overlay = _load(SECURITY_OVERLAY)
        vols = overlay.get("volumes", {})
        assert "postgres_data_security" in vols
        assert "redis_data_security" in vols
        assert "chromadb_data_security" in vols


# ---------------------------------------------------------------------------
# .env.security.example
# ---------------------------------------------------------------------------


class TestEnvSecurityExample:
    """The .env file must set the domain switch and use isolated ports."""

    @pytest.fixture
    def env_lines(self) -> list[str]:
        return ENV_SECURITY_EXAMPLE.read_text(encoding="utf-8").splitlines()

    def test_security_domain_set(self, env_lines: list[str]) -> None:
        assert any(
            line.strip().startswith("GRIMOIRE_SECURITY__DOMAIN=security")
            for line in env_lines
        )

    def test_isolated_postgres_port(self, env_lines: list[str]) -> None:
        assert any(line.strip() == "POSTGRES_PORT=5433" for line in env_lines)

    def test_security_chromadb_collection(self, env_lines: list[str]) -> None:
        assert any(
            "security_grimoire" in line
            for line in env_lines
            if line.startswith("GRIMOIRE_VECTOR_STORE__CHROMADB__COLLECTION_NAME")
        )

    def test_security_chromadb_port(self, env_lines: list[str]) -> None:
        # Avoid collision with the base compose's CHROMADB_PORT=8000.
        assert any(line.strip() == "CHROMADB_PORT=8002" for line in env_lines)

    def test_no_committed_secrets(self) -> None:
        """No real keys or production passwords in the example file."""
        text = ENV_SECURITY_EXAMPLE.read_text(encoding="utf-8")
        # The example file must contain CHANGE_ME-style placeholders, not
        # real-looking high-entropy secrets.
        assert "CHANGE_ME_BEFORE_DEPLOY" in text
        assert "changeme_secure_password" in text


# ---------------------------------------------------------------------------
# seed_corpus.sh
# ---------------------------------------------------------------------------


class TestSeedCorpusScript:
    def test_script_present(self) -> None:
        assert SEED_SCRIPT.is_file()

    def test_script_is_executable(self) -> None:
        # Stat mode bits: at least owner-execute.
        assert SEED_SCRIPT.stat().st_mode & 0o100, "seed_corpus.sh must be +x"

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
    def test_script_syntax(self) -> None:
        """``bash -n`` checks syntax without executing the script."""
        bash = shutil.which("bash")
        assert bash is not None  # already guarded by skipif, narrow the type
        result = subprocess.run(  # noqa: S603 — bash path resolved via shutil.which
            [bash, "-n", str(SEED_SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_script_uses_set_euo_pipefail(self) -> None:
        """Defensive scripting — short-circuit on errors / undef vars."""
        text = SEED_SCRIPT.read_text(encoding="utf-8")
        assert "set -euo pipefail" in text

    def test_script_references_three_corpora(self) -> None:
        """Plan calls for Sigma + NVD + MITRE ATT&CK."""
        text = SEED_SCRIPT.read_text(encoding="utf-8")
        assert "SigmaHQ/sigma" in text
        assert "mitre/cti" in text
        assert "nvd.nist.gov" in text


# ---------------------------------------------------------------------------
# Hetzner deploy doc
# ---------------------------------------------------------------------------


class TestHetznerDeployDoc:
    DOC = REPO_ROOT / "docs" / "deploy" / "hetzner_security.md"

    def test_doc_present(self) -> None:
        assert self.DOC.is_file()

    def test_doc_mentions_sizing_tiers(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        assert "CPX21" in text
        assert "CPX31" in text

    def test_doc_mentions_firewall(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        assert "firewall" in text.lower() or "ufw" in text.lower()

    def test_doc_mentions_backups(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        assert "backup" in text.lower()

    def test_doc_mentions_smoke_test_queries(self) -> None:
        text = self.DOC.read_text(encoding="utf-8")
        assert "smoke" in text.lower()
