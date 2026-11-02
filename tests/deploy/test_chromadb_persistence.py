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
# and CHROMA_PORT would break the healthcheck and every client. Every 0.x
# variable the service used to set (ANONYMIZED_TELEMETRY included) is ignored
# by 1.5.9, so nothing is allowed today; anything new deserves a deliberate look.
ALLOWED_ENV: set[str] = set()
# Keys that can change where the server persists, or what it mounts, without
# showing up in this service's own `volumes` or `environment`: the 1.x binary
# takes `run <config>` and path flags; configs/secrets can replace the
# /config.yaml that sets persist_path; tmpfs can shadow it or the data dir;
# extends inherits all of the above from another service unseen.
STARTUP_OVERRIDES = (
    "command",
    "entrypoint",
    "env_file",
    "configs",
    "secrets",
    "tmpfs",
    "extends",
    # build replaces the whole image, and with it the default /config.yaml.
    "build",
)
# The image's default command is `chroma run /config.yaml`.
CHROMA_CONFIG_FILE = "/config.yaml"
# Every name Compose loads by default or that this repo uses for overlays.
# A file outside the repo root, or one passed only via `-f <name>` or the
# COMPOSE_FILE variable, can't be discovered here; add it to COMPOSE_FILES.
COMPOSE_GLOBS = (
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose*.yml",
    "compose*.yaml",
)


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


def _guard_violations(service: dict) -> list[str]:
    """Everything about a chromadb service that could move or hide its data."""
    problems: list[str] = []
    targets = {target for _, target in _volume_mounts(service)}
    if LEGACY_DATA_DIR in targets:
        problems.append(f"mounts the 0.x path {LEGACY_DATA_DIR}")
    if CHROMA_CONFIG_FILE in targets:
        problems.append(f"mounts over {CHROMA_CONFIG_FILE}, which sets persist_path")
    extra_env = _env_names(service) - ALLOWED_ENV
    if extra_env:
        problems.append(f"sets environment {sorted(extra_env)}")
    problems += [f"sets {key!r}" for key in STARTUP_OVERRIDES if key in service]
    return problems


@pytest.mark.parametrize("filename", FILENAMES)
def test_no_way_to_move_or_hide_the_data(filename: str) -> None:
    assert _guard_violations(_chromadb_service(filename)) == []


# Written out literally, not generated from STARTUP_OVERRIDES: deleting a key
# from that tuple must make its probe here fail, not vanish with it. Each probe
# names the one violation it must produce, so it can't pass for another reason.
BYPASS_PROBES = {
    "legacy-path": ({"volumes": ["chromadb_data:/chroma/chroma"]}, "0.x path"),
    "config-mount": ({"volumes": ["./evil.yml:/config.yaml:ro"]}, "/config.yaml"),
    "persist-path-env": (
        {"environment": ["CHROMA_PERSIST_PATH=/elsewhere"]},
        "CHROMA_PERSIST_PATH",
    ),
    "0x-env-mapping": ({"environment": {"IS_PERSISTENT": "TRUE"}}, "IS_PERSISTENT"),
    "command": ({"command": ["run", "/other.yaml"]}, "'command'"),
    "entrypoint": ({"entrypoint": ["chroma"]}, "'entrypoint'"),
    "env_file": ({"env_file": [".env.chroma"]}, "'env_file'"),
    "configs": ({"configs": ["chroma_config"]}, "'configs'"),
    "secrets": ({"secrets": ["chroma_config"]}, "'secrets'"),
    "tmpfs": ({"tmpfs": ["/data"]}, "'tmpfs'"),
    "extends": ({"extends": {"service": "base"}}, "'extends'"),
    "build": ({"build": "."}, "'build'"),
}


@pytest.mark.parametrize(
    ("service", "expected"), BYPASS_PROBES.values(), ids=BYPASS_PROBES.keys()
)
def test_guards_catch_each_bypass(service: dict, expected: str) -> None:
    """Without this, a parsing regression would make the test above pass vacuously."""
    problems = _guard_violations(service)
    assert len(problems) == 1, problems
    assert expected in problems[0]


def test_every_startup_override_has_a_probe() -> None:
    probed = {key for svc, _ in BYPASS_PROBES.values() for key in svc}
    assert set(STARTUP_OVERRIDES) <= probed


def _load_compose(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def test_every_compose_file_with_chromadb_is_covered() -> None:
    """A new overlay that defines chromadb must be added to COMPOSE_FILES."""
    defining = set()
    paths = {p for pattern in COMPOSE_GLOBS for p in REPO_ROOT.glob(pattern)}
    for path in sorted(paths):
        compose = _load_compose(path)
        # include: pulls in other files whose services this scan never sees.
        assert "include" not in compose, f"{path.name} uses include:"
        if "chromadb" in (compose.get("services") or {}):
            defining.add(path.name)
    assert defining == set(FILENAMES)


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
