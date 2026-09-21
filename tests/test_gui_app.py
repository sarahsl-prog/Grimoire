"""Tests for GUI worker plumbing and the main window shell."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402

from grimoire.gui.app import MainWindow  # noqa: E402
from grimoire.gui.config import GuiConfig  # noqa: E402
from grimoire.gui.errors import AuthFailed, ConnectionFailed  # noqa: E402
from grimoire.gui.workers import ApiWorker  # noqa: E402


class _StubClient:
    """Stands in for GrimoireClient in window tests."""

    def __init__(self, config: GuiConfig) -> None:
        self.config = config
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TestApiWorker:
    def test_emits_result_on_success(self, qtbot) -> None:
        worker = ApiWorker(lambda: "done")
        with qtbot.waitSignal(worker.signals.finished, timeout=2000) as blocker:
            QThreadPool.globalInstance().start(worker)
        assert blocker.args == ["done"]

    def test_emits_gui_error_on_failure(self, qtbot) -> None:
        def boom() -> str:
            raise AuthFailed("API key rejected.")

        worker = ApiWorker(boom)
        with qtbot.waitSignal(worker.signals.failed, timeout=2000) as blocker:
            QThreadPool.globalInstance().start(worker)
        assert isinstance(blocker.args[0], AuthFailed)

    def test_wraps_unexpected_exception_as_gui_error(self, qtbot) -> None:
        from grimoire.gui.errors import GuiError

        def boom() -> str:
            raise RuntimeError("kaboom")

        worker = ApiWorker(boom)
        with qtbot.waitSignal(worker.signals.failed, timeout=2000) as blocker:
            QThreadPool.globalInstance().start(worker)
        error = blocker.args[0]
        assert isinstance(error, GuiError)
        assert "kaboom" not in error.message, "raw exception text must not leak"


class TestMainWindow:
    def test_opens_with_a_tab_area(self, qtbot) -> None:
        config = GuiConfig(base_url="http://testapi:8001", api_key="k")
        window = MainWindow(_StubClient(config), config)
        qtbot.addWidget(window)
        assert window.tabs is not None
        assert "Grimoire" in window.windowTitle()

    def test_shows_unconfigured_state_without_a_key(self, qtbot) -> None:
        config = GuiConfig(base_url="http://testapi:8001", api_key=None)
        window = MainWindow(_StubClient(config), config)
        qtbot.addWidget(window)
        assert "GRIMOIRE_API_KEY" in window.connection_bar.status_text()

    def test_status_bar_shows_errors(self, qtbot) -> None:
        config = GuiConfig(base_url="http://testapi:8001", api_key="k")
        window = MainWindow(_StubClient(config), config)
        qtbot.addWidget(window)
        window.show_error("Cannot reach the Grimoire API")
        assert "Cannot reach" in window.statusBar().currentMessage()

    def test_keeps_existing_client_when_new_key_is_malformed(
        self, qtbot, monkeypatch
    ) -> None:
        """A malformed base_url must not discard the working client.

        GrimoireClient.__init__ raises ConnectionFailed for a malformed
        base_url; _on_api_key_entered must catch that, keep the existing
        client and config untouched, and report the error instead.
        """
        config = GuiConfig(base_url="http://testapi:8001", api_key="k")
        original_client = _StubClient(config)
        window = MainWindow(original_client, config)
        qtbot.addWidget(window)

        def _raise_connection_failed(new_config: GuiConfig) -> None:
            raise ConnectionFailed(f"Invalid Grimoire API URL: {new_config.base_url}")

        monkeypatch.setattr(
            "grimoire.gui.client.GrimoireClient", _raise_connection_failed
        )

        window._on_api_key_entered("new-key")

        assert window.client is original_client
        assert window.config is config
        assert "Invalid Grimoire API URL" in window.statusBar().currentMessage()

    def test_retires_old_client_instead_of_closing_it(self, qtbot, monkeypatch) -> None:
        """A pasted key must not close the client an in-flight worker holds.

        _on_api_key_entered must move the outgoing client to
        _retired_clients rather than closing it immediately: an ApiWorker
        already running on the pool holds a closure over that exact
        instance, and closing it mid-request would fail that call.
        """
        config = GuiConfig(base_url="http://testapi:8001", api_key="k")
        original_client = _StubClient(config)
        window = MainWindow(original_client, config)
        qtbot.addWidget(window)

        monkeypatch.setattr("grimoire.gui.client.GrimoireClient", _StubClient)

        window._on_api_key_entered("new-key")

        assert original_client.closed is False
        assert original_client in window._retired_clients
        assert window.client is not original_client

    def test_close_event_only_closes_clients_once_pool_drains(
        self, qtbot, monkeypatch
    ) -> None:
        """closeEvent must not close clients while pool threads are alive.

        When waitForDone times out, a pool thread may still hold a closure
        over a client; closing it then would corrupt that worker's result.
        Only once the pool is confirmed drained should the current client
        and every retired client be closed.
        """
        config = GuiConfig(base_url="http://testapi:8001", api_key="k")
        original_client = _StubClient(config)
        window = MainWindow(original_client, config)
        qtbot.addWidget(window)

        retired_client = _StubClient(config)
        window._retired_clients.append(retired_client)

        monkeypatch.setattr(window.pool, "waitForDone", lambda ms: False)
        window.closeEvent(QCloseEvent())

        assert original_client.closed is False
        assert retired_client.closed is False

        monkeypatch.setattr(window.pool, "waitForDone", lambda ms: True)
        window.closeEvent(QCloseEvent())

        assert original_client.closed is True
        assert retired_client.closed is True


class TestMainEntryPoint:
    def test_returns_exit_code_one_on_malformed_url(self, monkeypatch) -> None:
        """A malformed GRIMOIRE_API_URL must exit cleanly, not crash.

        main() constructs a GrimoireClient before a QApplication exists;
        ConnectionFailed there must produce exit code 1 with a stderr
        message rather than an uncaught traceback.
        """
        from grimoire.gui import __main__ as gui_main

        monkeypatch.setenv("GRIMOIRE_API_URL", "not a valid url")

        def _raise_connection_failed(config: GuiConfig) -> None:
            raise ConnectionFailed(f"Invalid Grimoire API URL: {config.base_url}")

        monkeypatch.setattr(
            "grimoire.gui.client.GrimoireClient", _raise_connection_failed
        )

        exit_code = gui_main.main()

        assert exit_code == 1
