"""Tests for the app shell: tabs, status bar, health check, key bindings.

Run against a real Textual app in headless mode via ``App.run_test()``.  The
client is a stub, so no network and no terminal are involved.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from textual.containers import Vertical  # noqa: E402
from textual.widgets import Input, Static, TabbedContent  # noqa: E402

from grimoire.client.config import ClientConfig  # noqa: E402
from grimoire.tui.app import GrimoireApp  # noqa: E402
from grimoire.tui.messages import ConnectionReport  # noqa: E402
from grimoire.tui.widgets.status_bar import StatusBar  # noqa: E402

from .conftest import StubClient  # noqa: E402


class _RecordingPane(Static):
    """A pane that counts refreshes, standing in for a real one."""

    def __init__(self, label: str, pane_id: str) -> None:
        super().__init__(label, id=pane_id)
        self.refreshes = 0

    def refresh_data(self) -> None:
        self.refreshes += 1


class _TestApp(GrimoireApp):
    """GrimoireApp with recording panes in place of the real ones."""

    def make_search_pane(self) -> Any:
        return _RecordingPane("search", "search-pane")

    def make_documents_pane(self) -> Any:
        return _RecordingPane("documents", "documents-pane")


def _make(client: StubClient, **kwargs: Any) -> _TestApp:
    return _TestApp(client, client.config, kwargs.get("log_path"))  # type: ignore[arg-type]


async def _settled(app: GrimoireApp, pilot: Any) -> None:
    """Wait until every background worker has finished and messages are handled."""
    await app.workers.wait_for_complete()
    await pilot.pause()


class TestLayout:
    async def test_has_the_three_tabs_with_search_first(
        self, stub_client: StubClient
    ) -> None:
        app = _make(stub_client)
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)

            assert tabs.active == "search"
            assert {p.id for p in app.query("TabPane")} == {
                "search",
                "documents",
                "categories",
            }
            await _settled(app, pilot)

    async def test_f2_and_f1_switch_tabs(self, stub_client: StubClient) -> None:
        app = _make(stub_client)
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)

            await pilot.press("f2")
            assert tabs.active == "documents"
            await pilot.press("f1")
            assert tabs.active == "search"
            await _settled(app, pilot)

    async def test_question_mark_opens_the_help_panel(
        self, stub_client: StubClient
    ) -> None:
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await pilot.press("question_mark")
            await pilot.pause()

            assert app.screen.query("HelpPanel")
            await _settled(app, pilot)

    async def test_ctrl_q_quits(self, stub_client: StubClient) -> None:
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+q")
            await pilot.pause()

            assert not app.is_running


class TestHealthCheck:
    async def test_healthy_server_shows_connected(
        self, stub_client: StubClient
    ) -> None:
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            assert app.query_one(StatusBar).state == "connected"

    async def test_starts_in_the_checking_state(self, stub_client, step) -> None:
        gate = threading.Event()
        stub_client.health_script = [step(True, gate=gate)]
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await pilot.pause()

            assert app.query_one(StatusBar).state == "checking"
            gate.set()
            await _settled(app, pilot)
            assert app.query_one(StatusBar).state == "connected"

    async def test_unhealthy_server_shows_unreachable_without_crashing(
        self, stub_client: StubClient
    ) -> None:
        stub_client.healthy = False
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            assert app.query_one(StatusBar).state == "unreachable"
            assert app.is_running

    async def test_a_raising_health_check_is_contained(self, stub_client, step) -> None:
        """health() promises not to raise; a broken one must not kill the UI."""
        stub_client.health_script = [step(RuntimeError("boom /srv/secret"))]
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            assert app.query_one(StatusBar).state == "unreachable"
            assert app.is_running

    async def test_ctrl_r_rechecks_health_and_refreshes_the_active_pane(
        self, stub_client: StubClient
    ) -> None:
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            assert stub_client.health_calls == 1
            search = app.query_one("#search-pane", _RecordingPane)
            documents = app.query_one("#documents-pane", _RecordingPane)

            await pilot.press("ctrl+r")
            await _settled(app, pilot)

            assert stub_client.health_calls == 2
            assert (search.refreshes, documents.refreshes) == (1, 0)

            await pilot.press("f2", "ctrl+r")
            await _settled(app, pilot)
            assert (search.refreshes, documents.refreshes) == (1, 1)

    async def test_a_stale_health_result_cannot_overwrite_a_newer_one(
        self, stub_client, step
    ) -> None:
        """First check hangs and would say unreachable; the second says connected.

        Thread workers cannot be killed, so the old one finishes later and must
        have its result dropped.
        """
        gate = threading.Event()
        stub_client.health_script = [step(False, gate=gate), step(True)]
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()
            await pilot.pause()
            assert app.query_one(StatusBar).state == "connected"

            gate.set()  # the abandoned first check now returns False
            await _settled(app, pilot)

            assert app.query_one(StatusBar).state == "connected"


class TestConnectionReports:
    async def test_a_pane_can_flip_the_indicator_to_unreachable_and_back(
        self, stub_client: StubClient
    ) -> None:
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            pane = app.query_one("#search-pane", _RecordingPane)
            bar = app.query_one(StatusBar)

            pane.post_message(ConnectionReport(reachable=False))
            await pilot.pause()
            assert bar.state == "unreachable"

            pane.post_message(ConnectionReport(reachable=True))
            await pilot.pause()
            assert bar.state == "connected"


class TestApiKeyWarning:
    async def _notifications(self, app: GrimoireApp) -> list[tuple[str, str]]:
        recorded: list[tuple[str, str]] = []
        original = app.notify

        def spy(message: str, **kwargs: Any) -> None:
            recorded.append((message, str(kwargs.get("severity", "information"))))
            original(message, **kwargs)

        app.notify = spy  # type: ignore[method-assign]
        return recorded

    async def test_missing_key_warns_exactly_once(self) -> None:
        client = StubClient(config=ClientConfig(base_url="http://stub:8001"))
        app = _make(client)
        recorded = await self._notifications(app)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await pilot.press("ctrl+r")  # refreshing must not repeat the warning
            await _settled(app, pilot)

        warnings = [m for m, sev in recorded if sev == "warning"]
        assert len(warnings) == 1
        assert "GRIMOIRE_API_KEY" in warnings[0]

    async def test_configured_key_does_not_warn(self, stub_client: StubClient) -> None:
        app = _make(stub_client)
        recorded = await self._notifications(app)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

        assert recorded == []


class TestStatusBarContent:
    async def test_shows_url_key_state_and_connection(
        self, stub_client: StubClient
    ) -> None:
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            text = app.query_one(StatusBar).text
            assert "http://stub:8001" in text
            assert "key: set" in text
            assert "connected" in text

    async def test_key_missing_is_shown(self) -> None:
        client = StubClient(config=ClientConfig(base_url="http://stub:8001"))
        app = _make(client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            assert "key: missing" in app.query_one(StatusBar).text

    async def test_credentials_in_the_url_are_never_displayed(self) -> None:
        client = StubClient(
            config=ClientConfig(base_url="http://user:s3cret@stub:8001", api_key="k")
        )
        app = _make(client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            text = app.query_one(StatusBar).text
            assert "s3cret" not in text and "user" not in text
            assert "stub:8001" in text

    async def test_the_key_value_is_never_displayed(self) -> None:
        client = StubClient(
            config=ClientConfig(
                base_url="http://stub:8001", api_key="grim_agt_TOPSECRET"
            )
        )
        app = _make(client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            assert "TOPSECRET" not in app.query_one(StatusBar).text

    async def test_the_url_path_is_not_displayed(self) -> None:
        client = StubClient(
            config=ClientConfig(base_url="http://stub:8001/some/path", api_key="k")
        )
        app = _make(client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            assert "/some/path" not in app.query_one(StatusBar).text

    async def test_an_unparseable_url_cannot_take_the_bar_down(self) -> None:
        client = StubClient(config=ClientConfig(base_url="http://[red]:1", api_key="k"))
        app = _make(client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            assert app.is_running
            assert "(invalid URL)" in app.query_one(StatusBar).text

    async def test_ipv6_literal_is_shown_verbatim_not_eaten_as_markup(self) -> None:
        client = StubClient(
            config=ClientConfig(base_url="http://[::1]:8001", api_key="k")
        )
        app = _make(client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            bar = app.query_one(StatusBar)
            assert "[::1]:8001" in bar.text
            assert "[::1]:8001" in app.export_screenshot()


class TestClientOwnership:
    async def test_the_app_does_not_close_the_client(
        self, stub_client: StubClient
    ) -> None:
        """The caller that built the client closes it (see ``main``), so a
        second close here would be a double close."""
        app = _make(stub_client)
        async with app.run_test() as pilot:
            await _settled(app, pilot)

        assert stub_client.closed == 0


class TestWithTheRealClient:
    """End to end through httpx and a real socket; no stubbed client."""

    @staticmethod
    def _serve(status: int) -> tuple[Any, int]:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args: Any) -> None:  # keep test output quiet
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, server.server_address[1]

    async def test_a_listening_server_shows_connected(self) -> None:
        from grimoire.client.client import GrimoireClient

        server, port = self._serve(200)
        config = ClientConfig(base_url=f"http://127.0.0.1:{port}", api_key="k")
        client = GrimoireClient(config)
        try:
            app = GrimoireApp(client, config, None)
            async with app.run_test() as pilot:
                await _settled(app, pilot)

                assert app.query_one(StatusBar).state == "connected"
        finally:
            client.close()
            server.shutdown()

    async def test_a_server_answering_500_shows_unreachable(self) -> None:
        from grimoire.client.client import GrimoireClient

        server, port = self._serve(500)
        config = ClientConfig(base_url=f"http://127.0.0.1:{port}", api_key="k")
        client = GrimoireClient(config)
        try:
            app = GrimoireApp(client, config, None)
            async with app.run_test() as pilot:
                await _settled(app, pilot)

                assert app.query_one(StatusBar).state == "unreachable"
        finally:
            client.close()
            server.shutdown()

    async def test_nothing_listening_shows_unreachable(self) -> None:
        import socket

        from grimoire.client.client import GrimoireClient

        with socket.socket() as sock:  # reserve a port, then free it
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = ClientConfig(base_url=f"http://127.0.0.1:{port}", api_key="k")
        client = GrimoireClient(config)
        try:
            app = GrimoireApp(client, config, None)
            async with app.run_test() as pilot:
                await _settled(app, pilot)

                assert app.query_one(StatusBar).state == "unreachable"
                assert app.is_running
        finally:
            client.close()


class _FocusingPane(Vertical):
    """A pane that takes the keyboard when shown, like the real ones do."""

    def __init__(self, pane_id: str) -> None:
        super().__init__(id=pane_id)
        self.focus_calls = 0
        self.shown_calls = 0

    def compose(self) -> Any:
        yield Input(id=f"{self.id}-input")

    def focus_primary(self) -> None:
        self.focus_calls += 1
        self.query_one(Input).focus()

    def tab_shown(self) -> None:
        self.shown_calls += 1


class _FocusingApp(GrimoireApp):
    def make_search_pane(self) -> Any:
        return _FocusingPane("search-pane")

    def make_documents_pane(self) -> Any:
        return _FocusingPane("documents-pane")


class TestTabSwitchingDoesNotFight:
    """Regression: focusing a widget inside a pane makes TabbedContent activate
    that pane's tab.  Hook callbacks queued for a tab the user has already left
    used to run late, steal focus back, and flip the tab again, so two panes
    that each take the keyboard ping-ponged forever (over a hundred
    activations, tens of seconds of churn)."""

    async def test_switching_tabs_settles_instead_of_ping_ponging(
        self, stub_client: StubClient
    ) -> None:
        app = _FocusingApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await pilot.press("f2")
            for _ in range(3):
                await pilot.pause()

            search = app.query_one("#search-pane", _FocusingPane)
            documents = app.query_one("#documents-pane", _FocusingPane)
            assert app.query_one(TabbedContent).active == "documents"
            assert app.focused is app.query_one("#documents-pane-input")
            assert search.focus_calls <= 1
            assert documents.focus_calls == 1
            assert documents.shown_calls == 1

    async def test_the_pane_for_the_tab_the_user_left_is_not_told_it_is_shown(
        self, stub_client: StubClient
    ) -> None:
        app = _FocusingApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            for _ in range(3):  # let the startup activation finish
                await pilot.pause()
            search = app.query_one("#search-pane", _FocusingPane)
            before = search.shown_calls

            # No awaits between these, so every deferred callback is still queued
            # when the user has already ended up on Documents.
            app.action_show_tab("documents")
            app.action_show_tab("search")
            app.action_show_tab("documents")
            for _ in range(3):
                await pilot.pause()

            assert app.query_one(TabbedContent).active == "documents"
            assert search.shown_calls == before


class TestTabSwitchIsReliable:
    """Regression: pressing F2 from the query box sometimes undid itself.

    When the pane being left is hidden, Textual refocuses a widget inside it,
    and that late focus event makes ``TabbedContent`` activate the pane that was
    just left.  Roughly one try in six ended back where it started.
    """

    async def test_focus_is_dropped_before_the_switch(
        self, stub_client: StubClient
    ) -> None:
        """The mechanism, deterministically: nothing is left focused in the pane
        being left, so there is nothing for Textual to refocus."""
        app = _FocusingApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            for _ in range(6):
                await pilot.pause()
            assert app.focused is not None  # the Search box has the keyboard

            app.action_show_tab("documents")

            assert app.focused is None  # synchronously, before any event runs

    async def test_switching_to_the_active_tab_is_a_no_op(
        self, stub_client: StubClient
    ) -> None:
        app = _FocusingApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            for _ in range(6):
                await pilot.pause()
            focused = app.focused

            app.action_show_tab("search")  # already there
            await pilot.pause()

            assert app.focused is focused  # not needlessly dropped

    async def test_f2_from_the_query_box_always_lands_on_documents(
        self, stub_client: StubClient
    ) -> None:
        """The behaviour, repeated: before the fix about one trial in six failed."""
        for _ in range(8):
            app = _FocusingApp(stub_client, stub_client.config, None)
            async with app.run_test() as pilot:
                for _ in range(6):
                    await pilot.pause()
                await pilot.press("f2")
                for _ in range(10):
                    await pilot.pause(0.02)

                assert app.query_one(TabbedContent).active == "documents"
                assert app.focused is app.query_one("#documents-pane-input")


class TestHooksRunOncePerSwitch:
    """Regression: Textual can report one switch twice (it briefly activates the
    tab the focus just left, then the right one again).  Hooks such as
    ``tab_shown()`` do real work, like retrying a failed load, so running them
    twice turned one tab switch into two requests."""

    async def test_the_same_pane_arriving_twice_in_a_row_runs_its_hooks_once(
        self, stub_client: StubClient
    ) -> None:
        app = _FocusingApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            for _ in range(6):
                await pilot.pause()
            app.action_show_tab("documents")
            for _ in range(6):
                await pilot.pause()
            documents = app.query_one("#documents-pane", _FocusingPane)
            pane = app.query_one(TabbedContent).active_pane
            assert documents.shown_calls == 1

            app._pane_shown(pane)  # the duplicate report
            app._pane_shown(pane)

            assert documents.shown_calls == 1
            assert documents.focus_calls == 1

    async def test_going_away_and_coming_back_runs_the_hooks_again(
        self, stub_client: StubClient
    ) -> None:
        """The dedupe must not swallow a genuine second visit."""
        app = _FocusingApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            for _ in range(6):
                await pilot.pause()
            documents = app.query_one("#documents-pane", _FocusingPane)

            for tab in ("documents", "search", "documents"):
                app.action_show_tab(tab)
                for _ in range(6):
                    await pilot.pause()

            assert documents.shown_calls == 2
