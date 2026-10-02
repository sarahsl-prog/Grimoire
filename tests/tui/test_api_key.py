"""Entering the API key inside the app (B2).

The key is a credential: these tests check not just that it works but that it
is applied in memory only, is never shown or logged, and that a failure leaves
the app on its working client.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from loguru import logger  # noqa: E402
from textual.widgets import Input, TabbedContent  # noqa: E402

from grimoire.client.config import ClientConfig  # noqa: E402
from grimoire.client.errors import ConnectionFailed  # noqa: E402
from grimoire.tui.app import GrimoireApp  # noqa: E402
from grimoire.tui.screens.api_key import ApiKeyScreen, parse_api_key  # noqa: E402
from grimoire.tui.widgets.status_bar import StatusBar  # noqa: E402

from .conftest import StubClient, screen_text  # noqa: E402

KEY = "grim_agt_secretvalue1234567890"


class _Factory:
    """A client factory that records configs and hands out StubClients."""

    def __init__(
        self,
        fail: Exception | None = None,
        prepare: Callable[[StubClient], None] | None = None,
    ) -> None:
        self.configs: list[ClientConfig] = []
        self.clients: list[StubClient] = []
        self.fail = fail
        self.prepare = prepare

    def __call__(self, config: ClientConfig) -> Any:
        if self.fail is not None:
            raise self.fail
        self.configs.append(config)
        client = StubClient(config=config)
        if self.prepare is not None:
            self.prepare(client)  # script it before the app starts calling it
        self.clients.append(client)
        return client


def _app(client: StubClient, factory: _Factory) -> GrimoireApp:
    return GrimoireApp(client, client.config, None, client_factory=factory)  # type: ignore[arg-type]


async def _settled(app: GrimoireApp, pilot: Any) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def _enter_key(app: GrimoireApp, pilot: Any, text: str) -> None:
    """Open the modal, type ``text`` and press Enter."""
    await pilot.press("ctrl+k")
    await pilot.pause()
    app.screen.query_one("#apikey-input", Input).value = text
    await pilot.press("enter")
    await _settled(app, pilot)


class TestParseApiKey:
    @pytest.mark.parametrize(
        "text", ["grim_dvl_abc123", "a", "x" * 256, "  padded-key  ", "k!@#$%^&*()"]
    )
    def test_accepts_printable_ascii_without_spaces(self, text: str) -> None:
        assert parse_api_key(text) == text.strip()

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "x" * 257,
            "two words",
            "line\nbreak",
            "tab\there",
            "trailing\n\nnewline-inside",
            "nul\x00byte",
            "café",
            "дом",
        ],
    )
    def test_rejects_everything_else(self, text: str) -> None:
        assert parse_api_key(text) is None


class TestOpening:
    async def test_ctrl_k_opens_the_modal(self, stub_client: StubClient) -> None:
        app = _app(stub_client, _Factory())
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await pilot.press("ctrl+k")
            await pilot.pause()

            assert isinstance(app.screen, ApiKeyScreen)
            assert app.screen.query_one("#apikey-input", Input).has_focus

    async def test_the_input_is_masked(self, stub_client: StubClient) -> None:
        app = _app(stub_client, _Factory())
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await pilot.press("ctrl+k")
            await pilot.pause()

            assert app.screen.query_one("#apikey-input", Input).password is True

    async def test_ctrl_k_works_while_the_query_box_has_the_keyboard(
        self, stub_client: StubClient
    ) -> None:
        """A focused Input binds Ctrl+K to delete-to-end; ours must win, and
        must not eat what was typed."""
        app = _app(stub_client, _Factory())
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            query = app.query_one("#query-input", Input)
            query.focus()
            query.value = "keep this text"
            await pilot.pause()
            await pilot.press("ctrl+k")
            await pilot.pause()

            assert isinstance(app.screen, ApiKeyScreen)
            assert query.value == "keep this text"

    async def test_a_second_ctrl_k_does_not_stack_a_modal(
        self, stub_client: StubClient
    ) -> None:
        app = _app(stub_client, _Factory())
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await pilot.press("ctrl+k")
            await pilot.pause()
            await pilot.press("ctrl+k")
            await pilot.pause()

            assert len(app.screen_stack) == 2  # the default screen and one modal

    async def test_the_footer_advertises_the_binding(
        self, stub_client: StubClient
    ) -> None:
        app = _app(stub_client, _Factory())
        async with app.run_test(size=(120, 30)) as pilot:
            await _settled(app, pilot)

            assert "API key" in screen_text(app)


class TestCancelling:
    async def test_escape_changes_nothing(self, stub_client: StubClient) -> None:
        factory = _Factory()
        app = _app(stub_client, factory)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await pilot.press("ctrl+k")
            await pilot.pause()
            field = app.screen.query_one("#apikey-input", Input)
            field.value = KEY
            await pilot.press("escape")
            await _settled(app, pilot)

            assert not isinstance(app.screen, ApiKeyScreen)
            assert factory.configs == []
            assert app._client is stub_client
            assert field.value == ""  # what was typed is not left in the widget

    async def test_the_cancel_button_changes_nothing(
        self, stub_client: StubClient
    ) -> None:
        factory = _Factory()
        app = _app(stub_client, factory)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await pilot.press("ctrl+k")
            await pilot.pause()
            await pilot.click("#apikey-cancel")
            await _settled(app, pilot)

            assert factory.configs == []
            assert not isinstance(app.screen, ApiKeyScreen)


class TestValidation:
    @pytest.mark.parametrize("bad", ["", "   ", "two words", "x" * 300, "café"])
    async def test_a_bad_key_is_explained_and_the_modal_stays_open(
        self, stub_client: StubClient, bad: str
    ) -> None:
        factory = _Factory()
        app = _app(stub_client, factory)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await _enter_key(app, pilot, bad)

            assert isinstance(app.screen, ApiKeyScreen)
            assert app.screen.error_text
            assert factory.configs == []

    async def test_the_error_never_repeats_what_was_typed(
        self, stub_client: StubClient
    ) -> None:
        app = _app(stub_client, _Factory())
        async with app.run_test(size=(120, 40)) as pilot:
            await _settled(app, pilot)
            await _enter_key(app, pilot, "bad key with spaces " + KEY)

            assert KEY not in app.screen.error_text
            assert KEY not in screen_text(app)

    async def test_typing_again_clears_the_error(self, stub_client: StubClient) -> None:
        app = _app(stub_client, _Factory())
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await _enter_key(app, pilot, "bad key")
            assert app.screen.error_text
            app.screen.query_one("#apikey-input", Input).value = "better"
            await pilot.pause()

            assert app.screen.error_text == ""


class TestApplying:
    async def test_the_new_client_is_built_from_the_old_config_plus_the_key(
        self, stub_client: StubClient
    ) -> None:
        stub_client.config = ClientConfig(
            base_url="http://stub:8001", api_key=None, session_id="launch-1"
        )
        factory = _Factory()
        app = _app(stub_client, factory)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await _enter_key(app, pilot, f"  {KEY}  ")

            (config,) = factory.configs
            assert config.api_key == KEY  # surrounding whitespace removed
            assert config.base_url == "http://stub:8001"
            assert config.session_id == "launch-1"
            assert not isinstance(app.screen, ApiKeyScreen)

    async def test_the_panes_switch_to_the_new_client(
        self, stub_client: StubClient
    ) -> None:
        factory = _Factory()
        app = _app(stub_client, factory)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await _enter_key(app, pilot, KEY)

            (new_client,) = factory.clients
            assert app._client is new_client
            assert app.query_one("#search-pane")._client is new_client
            assert app.query_one("#documents-pane")._client is new_client

    async def test_a_question_after_the_change_uses_the_new_client(
        self, stub_client: StubClient, step: Any
    ) -> None:
        from grimoire.api.schemas import QueryResponse

        def prepare(client: StubClient) -> None:
            client.ask_script = [step(QueryResponse(query="q", answer="ok"))]

        factory = _Factory(prepare=prepare)
        app = _app(stub_client, factory)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await _enter_key(app, pilot, KEY)
            query = app.query_one("#query-input", Input)
            query.focus()
            query.value = "what is sigma"
            await pilot.press("enter")
            await _settled(app, pilot)

        (new_client,) = factory.clients
        assert new_client.calls_to("ask")
        assert stub_client.calls_to("ask") == []

    async def test_the_status_bar_says_a_key_is_set_and_never_shows_it(
        self,
    ) -> None:
        client = StubClient(config=ClientConfig(base_url="http://stub:8001"))
        app = _app(client, _Factory())
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            assert "key: missing" in app.query_one(StatusBar).text
            await _enter_key(app, pilot, KEY)

            text = app.query_one(StatusBar).text
            assert "key: set" in text
            assert KEY not in text

    async def test_health_is_rechecked_on_the_new_client(
        self, stub_client: StubClient
    ) -> None:
        factory = _Factory()
        app = _app(stub_client, factory)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await _enter_key(app, pilot, KEY)

            assert factory.clients[0].health_calls >= 1
            assert app.query_one(StatusBar).state == "connected"

    async def test_the_visible_documents_pane_reloads_with_the_new_key(
        self, stub_client: StubClient, step: Any
    ) -> None:
        """The 401 that prompted the new key would otherwise sit there until
        the user thought to press Ctrl+R."""
        from grimoire.api.schemas import DocumentListResponse

        def prepare(client: StubClient) -> None:
            client.documents_script = [step(DocumentListResponse())]

        stub_client.documents_script = [step(DocumentListResponse())]
        factory = _Factory(prepare=prepare)
        app = _app(stub_client, factory)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await pilot.press("f2")
            await _settled(app, pilot)
            assert app.query_one(TabbedContent).active == "documents"
            assert len(stub_client.calls_to("list_documents")) == 1
            await _enter_key(app, pilot, KEY)

        (new_client,) = factory.clients
        assert len(new_client.calls_to("list_documents")) == 1
        assert len(stub_client.calls_to("list_documents")) == 1  # not asked again


class TestFailureLeavesTheAppWorking:
    async def test_a_factory_failure_keeps_the_old_client_and_config(
        self, stub_client: StubClient
    ) -> None:
        factory = _Factory(fail=ConnectionFailed("Invalid Grimoire API URL: x"))
        app = _app(stub_client, factory)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await _enter_key(app, pilot, KEY)

            assert app._client is stub_client
            assert app._config is stub_client.config
            assert app.is_running
            assert "key: set" in app.query_one(StatusBar).text  # unchanged


class TestClientLifecycle:
    async def test_the_app_closes_the_clients_it_built_but_not_the_one_it_was_given(
        self, stub_client: StubClient
    ) -> None:
        factory = _Factory()
        app = _app(stub_client, factory)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await _enter_key(app, pilot, KEY)
            await _enter_key(app, pilot, KEY + "2")
            # Retired, not closed, while the app runs: a worker may hold it.
            assert [c.closed for c in factory.clients] == [0, 0]

        assert [c.closed for c in factory.clients] == [1, 1]
        assert stub_client.closed == 0  # the caller's to close


class TestTheKeyStaysInMemory:
    async def test_it_is_never_logged_shown_or_written(
        self, stub_client: StubClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        lines: list[str] = []
        hid = logger.add(lines.append, level="DEBUG", format="{message}")
        factory = _Factory()
        app = _app(stub_client, factory)
        notifications: list[str] = []
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await _settled(app, pilot)
                await _enter_key(app, pilot, KEY)
                notifications = [n.message for n in app._notifications]
                shown = screen_text(app)
        finally:
            logger.remove(hid)

        assert KEY not in "\n".join(lines)
        assert all(KEY not in n for n in notifications)
        assert KEY not in shown
        # Nothing was written anywhere under the working directory.
        written = [p for p in tmp_path.rglob("*") if p.is_file()]
        assert all(KEY not in p.read_text(errors="ignore") for p in written)
        assert not (tmp_path / ".env").exists()

    async def test_the_input_is_emptied_once_the_key_is_taken(
        self, stub_client: StubClient
    ) -> None:
        app = _app(stub_client, _Factory())
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await pilot.press("ctrl+k")
            await pilot.pause()
            screen = app.screen
            field = screen.query_one("#apikey-input", Input)
            field.value = KEY
            await pilot.press("enter")
            await _settled(app, pilot)

            assert field.value == ""


class TestMissingKeyWarning:
    async def test_the_launch_warning_points_at_ctrl_k(self) -> None:
        client = StubClient(config=ClientConfig(base_url="http://stub:8001"))
        app = _app(client, _Factory())
        async with app.run_test() as pilot:
            await _settled(app, pilot)

            assert any("Ctrl+K" in n.message for n in app._notifications)
