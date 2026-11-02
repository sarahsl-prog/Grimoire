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
IGNORED_0X_SETTINGS = {
    "IS_PERSISTENT",
    "PERSIST_DIRECTORY",
    "CHROMA_SERVER_HOST",
    "CHROMA_SERVER_HTTP_PORT",
}


def _chromadb_service(filename: str) -> dict:
    with (REPO_ROOT / filename).open(encoding="utf-8") as f:
        compose = yaml.safe_load(f)
    service: dict = compose["services"]["chromadb"]
    return service


def _volume_targets(service: dict) -> dict[str, str]:
    """Map each volume source to its container path (short syntax only)."""
    targets: dict[str, str] = {}
    for entry in service.get("volumes", []):
        assert isinstance(entry, str), f"chromadb volume uses long syntax: {entry!r}"
        assert ":" in entry, f"chromadb volume has no container path: {entry!r}"
        source, target = entry.split(":")[:2]
        targets[source] = target
    return targets


def _env_names(service: dict) -> set[str]:
    env = service.get("environment", [])
    if isinstance(env, dict):
        return set(env)
    return {item.split("=", 1)[0] for item in env}


@pytest.mark.parametrize(("filename", "volume"), COMPOSE_FILES)
def test_data_volume_mounts_where_chroma_1x_persists(
    filename: str, volume: str
) -> None:
    targets = _volume_targets(_chromadb_service(filename))
    assert targets.get(volume) == CHROMA_DATA_DIR


@pytest.mark.parametrize("filename", FILENAMES)
def test_nothing_mounts_at_the_legacy_0x_path(filename: str) -> None:
    targets = _volume_targets(_chromadb_service(filename))
    assert LEGACY_DATA_DIR not in targets.values()


@pytest.mark.parametrize("filename", FILENAMES)
def test_no_ignored_0x_settings(filename: str) -> None:
    """These look like they configure Chroma but do nothing on 1.x."""
    service = _chromadb_service(filename)
    assert not _env_names(service) & IGNORED_0X_SETTINGS
    # An env_file would let the same variables back in unseen by this test.
    assert "env_file" not in service


@pytest.mark.parametrize("filename", FILENAMES)
def test_image_is_chroma_1x(filename: str) -> None:
    """The /data path is a 1.x fact; revisit these tests if the image changes.

    An overlay that sets no image inherits the base file's.
    """
    image = (
        _chromadb_service(filename).get("image")
        or _chromadb_service(BASE_COMPOSE)["image"]
    )
    assert image.startswith("chromadb/chroma:1.")
