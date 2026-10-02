"""Tests for the ``grimoire-tui`` entry point.

Textual and the app module are stubbed, so these run without Textual installed
and without a terminal.  What is under test is the sequencing in ``main``:
dependency check, logging, config, client, app, cleanup.
"""

from __future__ import annotations

import re
import sys
import tomllib
import types
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from grimoire.tui.__main__ import main

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


class _RecorderApp:
    """Stands in for GrimoireApp; records construction and run."""

    instances: list[_RecorderApp] = []
    run_error: Exception | None = None
    next_return_code = 0

    def __init__(self, client: Any, config: Any, log_path: Path | None) -> None:
        self.client = client
        self.config = config
        self.log_path = log_path
        self.ran = False
        self.return_code = type(self).next_return_code
        type(self).instances.append(self)

    def run(self) -> None:
        self.ran = True
        if type(self).run_error is not None:
            raise type(self).run_error


@pytest.fixture(autouse=True)
def tui_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> type[_RecorderApp]:
    """Stub Textual and the app, isolate cwd (logs) and the environment."""
    monkeypatch.setitem(sys.modules, "textual", types.ModuleType("textual"))
    fake_app_module = types.ModuleType("grimoire.tui.app")
    fake_app_module.GrimoireApp = _RecorderApp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "grimoire.tui.app", fake_app_module)
    monkeypatch.chdir(tmp_path)  # the default log dir is ./logs
    monkeypatch.delenv("GRIMOIRE_API_URL", raising=False)
    monkeypatch.delenv("GRIMOIRE_API_KEY", raising=False)
    _RecorderApp.instances = []
    _RecorderApp.run_error = None
    _RecorderApp.next_return_code = 0
    yield _RecorderApp
    logger.remove()
    logger.add(sys.stderr)


class TestArguments:
    def test_version_prints_and_returns_zero(self, capsys) -> None:
        assert main(["--version"]) == 0

        assert re.search(r"grimoire-tui \d+\.\d+", capsys.readouterr().out)

    def test_unknown_option_returns_nonzero_without_launching(
        self, tui_env, capsys
    ) -> None:
        assert main(["--bogus"]) != 0

        assert tui_env.instances == []

    def test_help_returns_zero(self, capsys) -> None:
        assert main(["--help"]) == 0

        assert "--url" in capsys.readouterr().out


class TestDependencyCheck:
    def test_missing_textual_prints_install_hint_and_returns_one(
        self, tui_env, monkeypatch, capsys
    ) -> None:
        monkeypatch.setitem(sys.modules, "textual", None)  # makes import fail

        assert main([]) == 1

        err = capsys.readouterr().err
        assert "uv sync --extra tui" in err
        assert "Traceback" not in err
        assert tui_env.instances == []

    def test_check_happens_before_logging_or_config(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setitem(sys.modules, "textual", None)

        main([])

        assert not (tmp_path / "logs").exists()


class TestUrlHandling:
    @pytest.mark.parametrize(
        "bad", ["not a url", "localhost:8001", "ftp://host", "http://", "", "//host"]
    )
    def test_rejects_malformed_urls_without_launching(
        self, tui_env, capsys, bad: str
    ) -> None:
        assert main(["--url", bad]) == 1

        err = capsys.readouterr().err
        assert "Invalid Grimoire API URL" in err
        assert "Traceback" not in err
        assert tui_env.instances == []

    def test_rejects_a_malformed_url_from_the_environment(
        self, tui_env, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("GRIMOIRE_API_URL", "not a url")

        assert main([]) == 1

        assert tui_env.instances == []

    def test_url_option_overrides_the_environment(self, tui_env, monkeypatch) -> None:
        monkeypatch.setenv("GRIMOIRE_API_URL", "http://from-env:1")

        main(["--url", "http://from-flag:2/"])

        assert tui_env.instances[0].config.base_url == "http://from-flag:2"

    def test_environment_is_used_when_no_flag_is_given(
        self, tui_env, monkeypatch
    ) -> None:
        monkeypatch.setenv("GRIMOIRE_API_URL", "https://box:9000")

        main([])

        assert tui_env.instances[0].config.base_url == "https://box:9000"

    def test_api_key_comes_from_the_environment_only(
        self, tui_env, monkeypatch
    ) -> None:
        monkeypatch.setenv("GRIMOIRE_API_KEY", "grim_agt_x")

        main([])

        assert tui_env.instances[0].config.api_key == "grim_agt_x"

    def test_there_is_no_api_key_option(self, tui_env, capsys) -> None:
        """Command-line secrets leak into ps output and shell history."""
        assert main(["--api-key", "grim_agt_x"]) != 0

        assert tui_env.instances == []


class TestLaunch:
    def test_runs_the_app_and_returns_zero(self, tui_env) -> None:
        assert main([]) == 0

        assert len(tui_env.instances) == 1
        assert tui_env.instances[0].ran

    def test_propagates_the_apps_return_code(self, tui_env) -> None:
        tui_env.next_return_code = 3

        assert main([]) == 3

    def test_client_is_closed_after_a_normal_run(self, tui_env, monkeypatch) -> None:
        closed: list[bool] = []
        from grimoire.client.client import GrimoireClient

        original = GrimoireClient.close
        monkeypatch.setattr(
            GrimoireClient, "close", lambda self: (closed.append(True), original(self))
        )

        main([])

        assert closed == [True]

    def test_client_is_closed_even_when_the_app_raises(
        self, tui_env, monkeypatch
    ) -> None:
        closed: list[bool] = []
        from grimoire.client.client import GrimoireClient

        original = GrimoireClient.close
        monkeypatch.setattr(
            GrimoireClient, "close", lambda self: (closed.append(True), original(self))
        )
        tui_env.run_error = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            main([])

        assert closed == [True]

    def test_app_receives_the_log_path(self, tui_env, tmp_path) -> None:
        main([])

        assert tui_env.instances[0].log_path == tmp_path / "logs" / "grimoire-tui.log"


class TestSessionId:
    def test_config_carries_a_valid_session_id(self, tui_env) -> None:
        main([])

        session_id = tui_env.instances[0].config.session_id
        assert session_id is not None
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id)

    def test_client_sends_it_as_a_header(self, tui_env) -> None:
        main([])

        app = tui_env.instances[0]
        assert app.client._http.headers["X-Session-Id"] == app.config.session_id

    def test_each_launch_gets_a_fresh_id(self, tui_env) -> None:
        main([])
        main([])

        first, second = (a.config.session_id for a in tui_env.instances)
        assert first != second

    def test_log_lines_carry_the_same_id(self, tui_env, tmp_path) -> None:
        main([])

        logger.complete()
        text = (tmp_path / "logs" / "grimoire-tui.log").read_text()
        assert tui_env.instances[0].config.session_id in text


class TestPackaging:
    def test_script_and_extra_are_declared(self) -> None:
        data = tomllib.loads(PYPROJECT.read_text())

        assert (
            data["project"]["scripts"]["grimoire-tui"] == "grimoire.tui.__main__:main"
        )
        assert any(
            dep.startswith("textual")
            for dep in data["project"]["optional-dependencies"]["tui"]
        )

    def test_dev_extra_includes_textual_so_ci_runs_the_tui_tests(self) -> None:
        data = tomllib.loads(PYPROJECT.read_text())

        dev = data["project"]["optional-dependencies"]["dev"]
        assert any(dep.startswith("textual") for dep in dev)
