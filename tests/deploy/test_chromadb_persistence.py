"""ChromaDB persistence in the compose files.

The chromadb/chroma 1.x server ignores the 0.x ``IS_PERSISTENT``,
``PERSIST_DIRECTORY`` and ``CHROMA_SERVER_*`` variables and persists to
``persist_path`` from its ``/config.yaml``, which is ``/data``. With the volume
mounted at the old 0.x path (``/chroma/chroma``), vectors were written inside
the container and lost on every recreate while Postgres kept the documents, so
search silently returned nothing. Parsing the YAML catches a regression that is
otherwise only visible after data is gone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = "docker-compose.yml"

# (compose file, named volume that must hold Chroma's data)
COMPOSE_FILES = (
    (BASE_COMPOSE, "chromadb_data"),
    ("docker-compose.security.yml", "chromadb_data_security"),
)
FILENAMES = [name for name, _ in COMPOSE_FILES]

CHROMA_DATA_DIR = "/data"
LEGACY_DATA_DIR = "/chroma/chroma"
# An allowlist, not a denylist of the removed 0.x names: 1.x honours
# CHROMA_<KEY> overrides, so CHROMA_PERSIST_PATH would move data off the volume
# and CHROMA_PORT would break the healthcheck and every client. Anything new
# here deserves a deliberate look.
ALLOWED_ENV = {"ANONYMIZED_TELEMETRY"}


def _chromadb_service(filename: str) -> dict:
    with (REPO_ROOT / filename).open(encoding="utf-8") as f:
        compose = yaml.safe_load(f)
    service: dict = compose["services"]["chromadb"]
    return service


def _volume_mounts(service: dict) -> list[tuple[str, str]]:
    """Every (source, container path) pair, short syntax only.

    A list rather than a dict: one source may legitimately mount twice.
    """
    mounts: list[tuple[str, str]] = []
    # `volumes:` with no value parses as None; anything else must be a list.
    volumes = service.get("volumes")
    if volumes is None:
        return mounts
    assert isinstance(volumes, list), f"chromadb volumes is not a list: {volumes!r}"
    for entry in volumes:
        assert isinstance(entry, str), f"chromadb volume uses long syntax: {entry!r}"
        assert ":" in entry, f"chromadb volume has no container path: {entry!r}"
        source, target = entry.split(":")[:2]
        mounts.append((source, target))
    return mounts


def _env_names(service: dict) -> set[str]:
    # `environment:` with no value parses as None; otherwise a list or mapping.
    env = service.get("environment")
    if env is None:
        return set()
    if isinstance(env, dict):
        return set(env)
    assert isinstance(env, list), f"chromadb environment is malformed: {env!r}"
    return {item.split("=", 1)[0] for item in env}


class TestHelpers:
    """The parsers above, on shapes the real compose files don't contain today."""

    def test_empty_keys_parse_as_nothing(self) -> None:
        service = {"volumes": None, "environment": None}
        assert _volume_mounts(service) == []
        assert _env_names(service) == set()

    def test_one_source_mounted_twice_keeps_both(self) -> None:
        service = {"volumes": ["chromadb_data:/data", "chromadb_data:/backups:ro"]}
        assert _volume_mounts(service) == [
            ("chromadb_data", "/data"),
            ("chromadb_data", "/backups"),
        ]

    def test_mapping_environment(self) -> None:
        assert _env_names({"environment": {"ANONYMIZED_TELEMETRY": "FALSE"}}) == {
            "ANONYMIZED_TELEMETRY"
        }

    @pytest.mark.parametrize(
        "volumes",
        ["", ["chromadb_data"], [{"type": "volume", "target": "/data"}]],
    )
    def test_malformed_volumes_fail_loudly(self, volumes: object) -> None:
        with pytest.raises(AssertionError):
            _volume_mounts({"volumes": volumes})

    @pytest.mark.parametrize("environment", ["", 0])
    def test_malformed_environment_fails_loudly(self, environment: object) -> None:
        with pytest.raises(AssertionError):
            _env_names({"environment": environment})


@pytest.mark.parametrize(("filename", "volume"), COMPOSE_FILES)
def test_data_volume_mounts_where_chroma_1x_persists(
    filename: str, volume: str
) -> None:
    mounts = _volume_mounts(_chromadb_service(filename))
    assert (volume, CHROMA_DATA_DIR) in mounts


@pytest.mark.parametrize("filename", FILENAMES)
def test_nothing_mounts_at_the_legacy_0x_path(filename: str) -> None:
    mounts = _volume_mounts(_chromadb_service(filename))
    assert LEGACY_DATA_DIR not in {target for _, target in mounts}


@pytest.mark.parametrize("filename", FILENAMES)
def test_only_allowed_environment(filename: str) -> None:
    """No ignored 0.x settings, and no 1.x override that relocates data."""
    service = _chromadb_service(filename)
    assert _env_names(service) <= ALLOWED_ENV
    # An env_file would let variables in unseen by this test.
    assert "env_file" not in service


@pytest.mark.parametrize("filename", FILENAMES)
def test_image_is_chroma_1x(filename: str) -> None:
    """The /data path is a 1.x fact; revisit these tests if the image changes.

    docker-compose.security.yml is only ever applied as an overlay on the base
    file (see docs/deploy/hetzner_security.md), so with no image of its own it
    inherits the base file's.
    """
    image = (
        _chromadb_service(filename).get("image")
        or _chromadb_service(BASE_COMPOSE).get("image")
        or ""
    )
    assert image.startswith(
        "chromadb/chroma:1."
    ), f"chromadb image is {image!r}; /data is only correct for chromadb/chroma 1.x"
