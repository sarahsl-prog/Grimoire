"""Tests for the GUI entry point's session id (B1).

PySide6 is stubbed so these run without the ``gui`` extra: what is under test
is the wiring of the session id into the client's config, not Qt.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from grimoire.gui.config import GuiConfig
from grimoire.gui.errors import ConnectionFailed


@pytest.fixture
def fake_qt(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub PySide6 and the main window; record what main() builds."""
    seen: dict[str, Any] = {}

    class FakeApp:
        def __init__(self, argv: list[str]) -> None:
            pass

        def setApplicationName(self, name: str) -> None:  # noqa: N802
            pass

        def exec(self) -> int:
            return 0

    class FakeWindow:
        def __init__(self, client: Any, config: GuiConfig) -> None:
            seen["window_config"] = config

        def show(self) -> None:
            pass

    widgets = types.ModuleType("PySide6.QtWidgets")
    widgets.QApplication = FakeApp  # type: ignore[attr-defined]
    pyside = types.ModuleType("PySide6")
    pyside.QtWidgets = widgets  # type: ignore[attr-defined]
    app_mod = types.ModuleType("grimoire.gui.app")
    app_mod.MainWindow = FakeWindow  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", widgets)
    monkeypatch.setitem(sys.modules, "grimoire.gui.app", app_mod)

    def make_client(config: GuiConfig) -> object:
        seen["client_config"] = config
        return object()

    monkeypatch.setattr("grimoire.gui.client.GrimoireClient", make_client)
    monkeypatch.delenv("GRIMOIRE_API_URL", raising=False)
    monkeypatch.delenv("GRIMOIRE_API_KEY", raising=False)
    return seen


def test_client_config_carries_a_valid_session_id(fake_qt: dict[str, Any]) -> None:
    from grimoire.gui.__main__ import main

    assert main() == 0
    session_id = fake_qt["client_config"].session_id
    assert isinstance(session_id, str)
    # Re-validates through GuiConfig's own rule, so the header cannot be rejected
    # by the server's matching pattern.
    GuiConfig(session_id=session_id)


def test_window_gets_the_same_config_as_the_client(fake_qt: dict[str, Any]) -> None:
    from grimoire.gui.__main__ import main

    main()
    assert fake_qt["window_config"] is fake_qt["client_config"]


def test_session_id_matches_the_one_bound_to_the_log(
    fake_qt: dict[str, Any],
) -> None:
    """The id sent to the server must be the id in the GUI's own log lines."""
    from loguru import logger

    from grimoire.gui.__main__ import main

    lines: list[str] = []
    hid = logger.add(lines.append, format="{extra[session_id]}|{message}\n")
    try:
        main()
    finally:
        logger.remove(hid)
        logger.configure(extra={})
    sent = fake_qt["client_config"].session_id
    assert any(line.startswith(f"{sent}|Starting Grimoire GUI") for line in lines)


def test_each_launch_gets_a_new_id(fake_qt: dict[str, Any]) -> None:
    from grimoire.gui.__main__ import main

    main()
    first = fake_qt["client_config"].session_id
    main()
    assert fake_qt["client_config"].session_id != first


def test_env_settings_survive_adding_the_session_id(
    fake_qt: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from grimoire.gui.__main__ import main

    monkeypatch.setenv("GRIMOIRE_API_URL", "http://example.test:9000")
    monkeypatch.setenv("GRIMOIRE_API_KEY", "k")
    main()
    cfg = fake_qt["client_config"]
    assert (cfg.base_url, cfg.api_key) == ("http://example.test:9000", "k")


def test_bad_url_still_exits_1(
    fake_qt: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from grimoire.gui.__main__ import main

    def boom(config: GuiConfig) -> None:
        raise ConnectionFailed("Invalid Grimoire API URL")

    monkeypatch.setattr("grimoire.gui.client.GrimoireClient", boom)
    assert main() == 1
