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
