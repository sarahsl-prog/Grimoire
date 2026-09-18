"""Compose application-service tests.

Parsing rather than running: every failure below produces a container that
starts cleanly and then cannot reach anything, which is slow and confusing
to diagnose live but trivial to catch in the YAML.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"

APP_SERVICES = ("api", "mcp", "watcher")
LONG_RUNNING = APP_SERVICES


@pytest.fixture(scope="module")
def compose() -> dict:
    with BASE_COMPOSE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def services(compose: dict) -> dict:
    return compose["services"]


class TestServicesExist:
    @pytest.mark.parametrize("name", ("db-migrate", *APP_SERVICES))
    def test_service_defined(self, services: dict, name: str) -> None:
        assert name in services

    def test_migration_service_is_not_named_migrate(self, services: dict) -> None:
        """`grimoire migrate` means something else entirely — a vector-store stub."""
        assert "migrate" not in services


class TestStartupOrdering:
    def test_db_migrate_does_not_restart(self, services: dict) -> None:
        """A one-shot job that restarts would re-run Alembic in a loop."""
        assert str(services["db-migrate"]["restart"]) == "no"

    def test_db_migrate_runs_alembic(self, services: dict) -> None:
        command = services["db-migrate"]["command"]
        assert "alembic" in command
        assert "upgrade" in command
        assert "head" in command

    def test_db_migrate_waits_for_postgres(self, services: dict) -> None:
        depends = services["db-migrate"]["depends_on"]
        assert depends["postgres"]["condition"] == "service_healthy"

    @pytest.mark.parametrize("name", LONG_RUNNING)
    def test_app_waits_for_migrations_to_complete(
        self, services: dict, name: str
    ) -> None:
        """No app container may open a connection before the schema is current."""
        depends = services[name]["depends_on"]
        assert depends["db-migrate"]["condition"] == "service_completed_successfully"

    @pytest.mark.parametrize("name", LONG_RUNNING)
    def test_app_waits_for_chromadb_to_be_healthy(
        self, services: dict, name: str
    ) -> None:
        """Every app container talks to the shared chromadb service over HTTP.

        A real service_healthy gate here only works if chromadb's own
        healthcheck can actually succeed — see TestChromaHealthcheck below.
        """
        depends = services[name]["depends_on"]
        assert depends["chromadb"]["condition"] == "service_healthy"


class TestEnvironmentWiring:
    @pytest.fixture(scope="class")
    def app_env(self, services: dict) -> dict:
        return services["api"]["environment"]

    def test_database_points_at_the_postgres_service(self, app_env: dict) -> None:
        assert "@postgres:5432/" in app_env["GRIMOIRE_DATABASE__URL"]

    def test_chroma_uses_the_service_over_http(self, app_env: dict) -> None:
        """Setting host/port selects the HttpClient branch in the vector store."""
        assert app_env["GRIMOIRE_VECTOR_STORE__HOST"] == "chromadb"
        assert str(app_env["GRIMOIRE_VECTOR_STORE__PORT"]) == "8000"

    def test_redis_points_at_the_redis_service(self, app_env: dict) -> None:
        assert app_env["GRIMOIRE_REDIS__HOST"] == "redis"

    def test_config_path_is_absolute(self, app_env: dict) -> None:
        """GRIMOIRE_CONFIG defaults to a relative path; containers pin it."""
        assert app_env["GRIMOIRE_CONFIG"].startswith("/")

    @pytest.mark.parametrize("name", ("db-migrate", *APP_SERVICES))
    def test_no_service_points_at_localhost(self, services: dict, name: str) -> None:
        """localhost inside a container is the container, not the host."""
        env = services[name].get("environment", {})
        offenders = {
            key: value
            for key, value in env.items()
            # host.docker.internal is the deliberate route to the host's Ollama.
            if isinstance(value, str)
            and ("localhost" in value or "127.0.0.1" in value)
        }
        assert not offenders, f"{name} still points at localhost: {offenders}"


class TestOllamaWiring:
    @pytest.fixture(scope="class")
    def app_env(self, services: dict) -> dict:
        return services["api"]["environment"]

    def test_llm_url_reaches_the_host(self, app_env: dict) -> None:
        assert "host.docker.internal" in app_env["GRIMOIRE_LLM__URL"]

    def test_llm_url_has_no_v1_suffix(self, app_env: dict) -> None:
        """The agents append /api/generate; a /v1 base yields a 404."""
        assert "/v1" not in app_env["GRIMOIRE_LLM__URL"]

    @pytest.mark.parametrize("name", ("db-migrate", *APP_SERVICES))
    def test_host_gateway_mapping_present(self, services: dict, name: str) -> None:
        extra_hosts = services[name].get("extra_hosts", [])
        assert any("host-gateway" in entry for entry in extra_hosts)


class TestCommands:
    def test_api_serves_the_rest_app(self, services: dict) -> None:
        assert "grimoire.api.main:app" in services["api"]["command"]

    def test_mcp_serves_the_authenticated_app(self, services: dict) -> None:
        """Never the raw SSE app — that transport has no authentication."""
        assert "grimoire.mcp.app:app" in services["mcp"]["command"]

    def test_watcher_runs_the_watch_daemon(self, services: dict) -> None:
        command = services["watcher"]["command"]
        assert "watch" in command
        assert "start" in command


class TestVolumes:
    def test_model_cache_volume_declared(self, compose: dict) -> None:
        """Docling and sentence-transformers download weights on first use."""
        assert "model_cache" in compose["volumes"]

    @pytest.mark.parametrize("name", APP_SERVICES)
    def test_model_cache_mounted(self, services: dict, name: str) -> None:
        mounts = services[name]["volumes"]
        assert any(str(m).startswith("model_cache:") for m in mounts)

    def test_watcher_mounts_its_corpus_read_only(self, services: dict) -> None:
        mounts = [str(m) for m in services["watcher"]["volumes"]]
        watch_mounts = [m for m in mounts if "/data/watch" in m]
        assert watch_mounts, "watcher has no corpus mount"
        assert all(m.endswith(":ro") for m in watch_mounts)


class TestHealthchecks:
    @pytest.mark.parametrize("name", ("api", "mcp"))
    def test_http_services_have_healthchecks(self, services: dict, name: str) -> None:
        assert "healthcheck" in services[name]
        test = " ".join(services[name]["healthcheck"]["test"])
        assert "/health" in test


class TestChromaHealthcheck:
    """chromadb/chroma ships no curl, wget, or python — only a shell and
    coreutils — so its healthcheck can't shell out to an HTTP client the
    way api/mcp's do. Confirmed by inspecting the running image directly.
    Without a real, passing healthcheck here, `service_healthy` above is a
    permanent blocker: `docker compose up -d` would never start api, mcp,
    or watcher.
    """

    @pytest.fixture(scope="class")
    def healthcheck(self, services: dict) -> dict:
        assert "healthcheck" in services["chromadb"]
        return services["chromadb"]["healthcheck"]

    def test_does_not_rely_on_curl_wget_or_python(self, healthcheck: dict) -> None:
        test = " ".join(healthcheck["test"])
        for absent_binary in ("curl", "wget", "python"):
            assert absent_binary not in test, (
                f"chromadb image has no {absent_binary}; healthcheck would "
                "never pass and block every app service forever"
            )

    def test_uses_the_current_heartbeat_endpoint(self, healthcheck: dict) -> None:
        """/api/v1/heartbeat now returns 410 Gone; /api/v2 is current."""
        test = " ".join(healthcheck["test"])
        assert "/api/v2/heartbeat" in test
        assert "/api/v1/heartbeat" not in test

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
    def test_command_is_syntactically_valid_bash(self, healthcheck: dict) -> None:
        """The healthcheck shells out via `bash -c <script>`; that script
        itself must be valid, independent of whether chromadb is running.
        """
        test = healthcheck["test"]
        assert test[:3] == ["CMD", "bash", "-c"], test
        script = test[3]
        bash = shutil.which("bash")
        assert bash is not None  # already guarded by skipif, narrow the type
        result = subprocess.run(  # noqa: S603 — bash path resolved via shutil.which
            [bash, "-n", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
