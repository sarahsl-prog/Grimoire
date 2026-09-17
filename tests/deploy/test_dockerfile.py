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
