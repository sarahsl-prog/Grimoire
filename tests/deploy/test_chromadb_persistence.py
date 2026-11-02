"""ChromaDB persistence in the compose files.

The chromadb/chroma 1.x server ignores the 0.x ``IS_PERSISTENT`` and
``PERSIST_DIRECTORY`` variables and always persists to ``/data``. With the
volume mounted at the old 0.x path (``/chroma/chroma``), vectors were written
inside the container and lost on every recreate while Postgres kept the
documents, so search silently returned nothing. Parsing the YAML catches a
regression that is otherwise only visible after data is gone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# (compose file, named volume that must hold Chroma's data)
COMPOSE_FILES = (
    ("docker-compose.yml", "chromadb_data"),
    ("docker-compose.security.yml", "chromadb_data_security"),
)

CHROMA_DATA_DIR = "/data"


def _chromadb_service(filename: str) -> dict:
    with (REPO_ROOT / filename).open(encoding="utf-8") as f:
        compose = yaml.safe_load(f)
    service: dict = compose["services"]["chromadb"]
    return service


def _volume_targets(service: dict) -> dict[str, str]:
    """Map each named-volume/bind source to its container path (short syntax)."""
    targets: dict[str, str] = {}
    for entry in service.get("volumes", []):
        assert isinstance(entry, str), "chromadb volumes use short syntax"
        source, target = entry.split(":")[:2]
        targets[source] = target
    return targets


@pytest.mark.parametrize(("filename", "volume"), COMPOSE_FILES)
def test_data_volume_mounts_where_chroma_1x_persists(
    filename: str, volume: str
) -> None:
    targets = _volume_targets(_chromadb_service(filename))
    assert targets.get(volume) == CHROMA_DATA_DIR


@pytest.mark.parametrize(("filename", "volume"), COMPOSE_FILES)
def test_no_ignored_0x_persistence_settings(filename: str, volume: str) -> None:
    """These look like they configure persistence but do nothing on 1.x."""
    env = _chromadb_service(filename).get("environment", [])
    names = (
        {item.split("=", 1)[0] for item in env} if isinstance(env, list) else set(env)
    )
    assert not names & {"IS_PERSISTENT", "PERSIST_DIRECTORY"}


def test_image_is_chroma_1x() -> None:
    """The /data path is a 1.x fact; revisit these tests if the image changes."""
    image = _chromadb_service("docker-compose.yml")["image"]
    assert image.startswith("chromadb/chroma:1.")
