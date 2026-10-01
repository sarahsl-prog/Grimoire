"""Tests for the Search/Ask pane.

The pane talks to a stub client, so every outcome (answer, error, a call held
in flight) is scripted.  The properties that matter most here are the ones a
thread-based UI gets wrong: stale results, double submits, and untrusted text.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from textual.widgets import Input, Markdown, OptionList, Select  # noqa: E402

from grimoire.api.schemas import (  # noqa: E402
    CitationResponse,
    QueryResponse,
    SearchResponse,
    SearchResultItem,
)
from grimoire.gui.errors import (  # noqa: E402
    AuthFailed,
    ConnectionFailed,
    MalformedResponse,
    RateLimited,
    RequestRejected,
    ServerError,
    TimedOut,
)
from grimoire.tui.app import GrimoireApp  # noqa: E402
from grimoire.tui.errors import GENERIC_ERROR  # noqa: E402
from grimoire.tui.widgets.search_pane import SearchPane  # noqa: E402
from grimoire.tui.widgets.status_bar import StatusBar  # noqa: E402

from .conftest import screen_text  # noqa: E402


def _answer(text: str = "A detection rule format.", n: int = 2) -> QueryResponse:
    return QueryResponse(
        query="q",
        answer=text,
        model_used="llama3",
        cached=False,
        duration_ms=1200,
        citations=[
            CitationResponse(
                document_id=f"doc-{i}",
                document_title=f"Doc {i}",
                chunk_id=f"chunk-{i}",
                chunk_index=i,
                content_snippet=f"snippet text {i}",
                relevance_score=0.9 - i / 10,
            )
            for i in range(n)
        ],
    )


def _found(n: int = 2) -> SearchResponse:
    return SearchResponse(
        query="q",
        total_results=n,
        duration_ms=40,
        results=[
            SearchResultItem(
                chunk_id=f"chunk-{i}",
                document_id=f"doc-{i}",
                document_title=f"Found {i}",
                content=f"found content {i}",
                score=0.8 - i / 10,
            )
            for i in range(n)
        ],
    )


def _pane(app: GrimoireApp) -> SearchPane:
    return app.query_one(SearchPane)


async def _settled(app: GrimoireApp, pilot: Any) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def _submit(app: GrimoireApp, pilot: Any, query: str = "what is sigma") -> None:
    box = app.query_one("#query-input", Input)
    box.value = query
    box.focus()
    await pilot.press("enter")


def _sources(app: GrimoireApp) -> OptionList:
    return app.query_one("#source-list", OptionList)


class TestAsk:
    async def test_renders_answer_meta_and_sources(self, stub_client, step) -> None:
        stub_client.ask_script = [step(_answer())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot)
            await _settled(app, pilot)

            pane = _pane(app)
            assert pane.answer_text == "A detection rule format."
            assert _sources(app).option_count == 2
            assert [s.title for s in pane.sources] == ["Doc 0", "Doc 1"]
            assert "llama3" in pane.meta_text and "2 sources" in pane.meta_text
            assert not pane.in_flight

    async def test_calls_ask_with_the_query_top_k_and_no_filters(
        self, stub_client, step
    ) -> None:
        stub_client.ask_script = [step(_answer())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot, "  what is sigma  ")
            await _settled(app, pilot)

        ((args, kwargs),) = stub_client.calls_to("ask")
        assert args == ("what is sigma",)
        assert kwargs == {"top_k": 5, "filter_dict": None}

    async def test_an_empty_answer_gets_a_placeholder(self, stub_client, step) -> None:
        stub_client.ask_script = [step(_answer(text=""))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert "No answer" in _pane(app).answer_text

    async def test_no_sources_says_so(self, stub_client, step) -> None:
        stub_client.ask_script = [step(_answer(n=0))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert _sources(app).option_count == 0
            assert "No sources" in _pane(app).preview_text

    async def test_highlighting_a_source_updates_the_preview(
        self, stub_client, step
    ) -> None:
        stub_client.ask_script = [step(_answer())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot)
            await _settled(app, pilot)
            pane = _pane(app)
            assert "snippet text 0" in pane.preview_text  # first is auto-selected

            _sources(app).highlighted = 1
            await pilot.pause()

            assert "snippet text 1" in pane.preview_text
            assert "Doc 1" in pane.preview_text
            assert "doc-1" in pane.preview_text


class TestSearchMode:
    async def _search_mode(self, app: GrimoireApp, pilot: Any) -> None:
        app.query_one("#mode-search").value = True  # type: ignore[attr-defined]
        await pilot.pause()

    async def test_calls_search_and_hides_the_answer_area(
        self, stub_client, step
    ) -> None:
        stub_client.search_script = [step(_found())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await self._search_mode(app, pilot)
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert stub_client.calls_to("ask") == []
            assert len(stub_client.calls_to("search")) == 1
            assert not app.query_one("#answer", Markdown).display
            assert [s.title for s in _pane(app).sources] == ["Found 0", "Found 1"]
            assert "2 results" in _pane(app).meta_text

    async def test_switching_back_to_ask_shows_the_answer_area_again(
        self, stub_client, step
    ) -> None:
        stub_client.search_script = [step(_found())]
        stub_client.ask_script = [step(_answer())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await self._search_mode(app, pilot)
            await _submit(app, pilot)
            await _settled(app, pilot)

            app.query_one("#mode-ask").value = True  # type: ignore[attr-defined]
            await pilot.pause()
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert app.query_one("#answer", Markdown).display
            assert _pane(app).answer_text == "A detection rule format."


class TestTopK:
    async def test_a_valid_top_k_is_sent(self, stub_client, step) -> None:
        stub_client.ask_script = [step(_answer())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            app.query_one("#top-k", Input).value = "12"
            await _submit(app, pilot)
            await _settled(app, pilot)

        assert stub_client.calls_to("ask")[0][1]["top_k"] == 12

    @pytest.mark.parametrize("bad", ["0", "101", "x", "", "-3", "2.5"])
    async def test_an_invalid_top_k_blocks_the_request_and_is_not_clamped(
        self, stub_client, bad: str
    ) -> None:
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            top_k = app.query_one("#top-k", Input)
            top_k.value = bad
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert stub_client.calls == []
            assert not top_k.is_valid
            assert "top" in _pane(app).status_text.lower()


class TestFilters:
    async def test_filters_are_sent_exactly_as_build_filter_dict_would(
        self, stub_client, step
    ) -> None:
        stub_client.ask_script = [step(_answer())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            app.query_one("#tags-input", Input).value = "a, b"
            app.query_one("#source-type", Select).value = "playbook"
            app.query_one("#severity", Select).value = "high"
            app.query_one("#cve-input", Input).value = "CVE-2024-1"
            await _submit(app, pilot)
            await _settled(app, pilot)

        assert stub_client.calls_to("ask")[0][1]["filter_dict"] == {
            "tags": ["a", "b"],
            "severity": "high",
            "source_type": "playbook",
            "cve_id": "CVE-2024-1",
        }

    async def test_unset_filters_send_none_not_an_empty_dict(
        self, stub_client, step
    ) -> None:
        stub_client.ask_script = [step(_answer())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            app.query_one("#tags-input", Input).value = "  ,  "
            await _submit(app, pilot)
            await _settled(app, pilot)

        assert stub_client.calls_to("ask")[0][1]["filter_dict"] is None

    async def test_filters_apply_to_search_mode_too(self, stub_client, step) -> None:
        stub_client.search_script = [step(_found())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            app.query_one("#mode-search").value = True  # type: ignore[attr-defined]
            app.query_one("#severity", Select).value = "low"
            await _submit(app, pilot)
            await _settled(app, pilot)

        assert stub_client.calls_to("search")[0][1]["filter_dict"] == {
            "severity": "low"
        }


class TestInputValidation:
    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    async def test_an_empty_query_makes_no_request_and_says_why(
        self, stub_client, blank: str
    ) -> None:
        app = GrimoireApp(stub_client, stub_client.config, None)
        notes: list[str] = []
        original = app.notify
        app.notify = lambda m, **k: (notes.append(m), original(m, **k))[1]  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            await _submit(app, pilot, blank)
            await _settled(app, pilot)

        assert stub_client.calls == []
        assert any("Enter a question" in n for n in notes)


class TestInFlight:
    async def test_shows_progress_and_blocks_a_second_submit(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.ask_script = [step(_answer(), gate=gate)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot)
            await pilot.pause()
            pane = _pane(app)

            assert pane.in_flight
            assert "Working" in pane.status_text
            assert app.query_one("#run-button").disabled

            await _submit(app, pilot, "a different question")  # ignored
            await pilot.pause()
            assert len(stub_client.calls_to("ask")) == 1

            gate.set()
            await _settled(app, pilot)
            assert not pane.in_flight
            assert not app.query_one("#run-button").disabled

    async def test_the_abandon_binding_exists_only_while_a_request_is_running(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.ask_script = [step(_answer(), gate=gate)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            pane = _pane(app)
            assert pane.check_action("abandon", ()) is False

            await _submit(app, pilot)
            await pilot.pause()
            assert pane.check_action("abandon", ()) is True

            gate.set()
            await _settled(app, pilot)
            assert pane.check_action("abandon", ()) is False

    async def test_abandoning_frees_the_pane_and_says_the_server_may_still_work(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.ask_script = [step(_answer(), gate=gate)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot)
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()
            pane = _pane(app)

            assert not pane.in_flight
            assert "Abandoned" in pane.status_text
            assert "may still be working" in pane.status_text
            assert not app.query_one("#run-button").disabled
            gate.set()
            await _settled(app, pilot)

    async def test_a_late_result_from_an_abandoned_request_is_dropped(
        self, stub_client, step
    ) -> None:
        gate_a = threading.Event()
        stub_client.ask_script = [
            step(_answer("OLD ANSWER"), gate=gate_a),
            step(_answer("NEW ANSWER")),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot, "first")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await _submit(app, pilot, "second")
            await _settled(app, pilot)
            assert _pane(app).answer_text == "NEW ANSWER"

            gate_a.set()  # the abandoned call finally returns
            await _settled(app, pilot)

            assert _pane(app).answer_text == "NEW ANSWER"
            assert not _pane(app).in_flight

    async def test_an_abandoned_requests_late_error_is_dropped_too(
        self, stub_client, step
    ) -> None:
        gate_a = threading.Event()
        stub_client.ask_script = [
            step(ServerError("OLD FAILURE"), gate=gate_a),
            step(_answer("NEW ANSWER")),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot, "first")
            await pilot.pause()
            await pilot.press("escape")
            await _submit(app, pilot, "second")
            await _settled(app, pilot)

            gate_a.set()
            await _settled(app, pilot)

            assert "OLD FAILURE" not in _pane(app).status_text
            assert _pane(app).answer_text == "NEW ANSWER"


class TestErrors:
    @pytest.mark.parametrize(
        "error",
        [
            ConnectionFailed("Cannot reach the Grimoire API at http://x - is it up?"),
            TimedOut("The request timed out."),
            AuthFailed("API key rejected. Check GRIMOIRE_API_KEY."),
            RateLimited("Rate limited. Retry in 30s.", retry_after=30),
            ServerError("Grimoire API error - check the API logs."),
            MalformedResponse("Unexpected response from the API."),
            RequestRejected("Not found: nope"),
        ],
    )
    async def test_each_client_error_shows_its_own_message(
        self, stub_client, step, error
    ) -> None:
        stub_client.ask_script = [step(error)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert app.is_running
            assert _pane(app).status_text == error.message
            assert not _pane(app).in_flight
            assert not app.query_one("#run-button").disabled

    async def test_an_unexpected_exception_shows_the_generic_message_only(
        self, stub_client, step
    ) -> None:
        stub_client.ask_script = [step(RuntimeError("secret /srv/app/db.py line 9"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert app.is_running
            assert _pane(app).status_text == GENERIC_ERROR
            assert "secret" not in _pane(app).status_text

    async def test_previous_results_stay_visible_after_an_error(
        self, stub_client, step
    ) -> None:
        stub_client.ask_script = [step(_answer("KEEP ME")), step(ServerError("boom"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot, "one")
            await _settled(app, pilot)
            await _submit(app, pilot, "two")
            await _settled(app, pilot)

            assert _pane(app).answer_text == "KEEP ME"
            assert _sources(app).option_count == 2
            assert _pane(app).status_text == "boom"

    async def test_a_new_success_clears_the_old_error(self, stub_client, step) -> None:
        stub_client.ask_script = [step(ServerError("boom")), step(_answer())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _submit(app, pilot, "one")
            await _settled(app, pilot)
            await _submit(app, pilot, "two")
            await _settled(app, pilot)

            assert _pane(app).status_text == ""

    async def test_error_notifications_are_sent_with_markup_disabled(
        self, stub_client, step
    ) -> None:
        """Textual parses notification text as markup by default; an unmatched
        closing tag in a server message would raise inside the renderer."""
        hostile = "bad [/nonexistent] and [bold red]x"
        stub_client.ask_script = [step(RequestRejected(hostile))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        seen: list[dict[str, Any]] = []
        original = app.notify

        def spy(message: str, **kwargs: Any) -> None:
            seen.append({"message": message, **kwargs})
            original(message, **kwargs)

        app.notify = spy  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert app.is_running
        errors = [n for n in seen if n.get("severity") == "error"]
        assert errors and all(n.get("markup") is False for n in errors)


class TestConnectionReporting:
    async def test_a_connection_failure_flips_the_status_bar_to_unreachable(
        self, stub_client, step
    ) -> None:
        stub_client.ask_script = [step(ConnectionFailed("Cannot reach the API."))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            assert app.query_one(StatusBar).state == "connected"

            await _submit(app, pilot)
            await _settled(app, pilot)

            assert app.query_one(StatusBar).state == "unreachable"

    async def test_a_success_flips_it_back_to_connected(
        self, stub_client, step
    ) -> None:
        stub_client.healthy = False
        stub_client.ask_script = [step(_answer())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            assert app.query_one(StatusBar).state == "unreachable"

            await _submit(app, pilot)
            await _settled(app, pilot)

            assert app.query_one(StatusBar).state == "connected"

    @pytest.mark.parametrize(
        "error", [AuthFailed("no"), ServerError("no"), RateLimited("no")]
    )
    async def test_an_http_error_means_the_server_is_reachable(
        self, stub_client, step, error
    ) -> None:
        """A 401/429/500 is an answer, so the API is up."""
        stub_client.healthy = False
        stub_client.ask_script = [step(error)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert app.query_one(StatusBar).state == "connected"

    async def test_a_timeout_is_not_evidence_either_way(
        self, stub_client, step
    ) -> None:
        stub_client.ask_script = [step(TimedOut("slow"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _settled(app, pilot)
            assert app.query_one(StatusBar).state == "connected"

            await _submit(app, pilot)
            await _settled(app, pilot)

            assert app.query_one(StatusBar).state == "connected"


class TestUntrustedText:
    """Answers and titles come from an LLM and from ingested files."""

    HOSTILE = "[bold red]boom[/] [link=http://evil.example]click[/link] [/nonexistent]"

    async def test_hostile_titles_and_chunk_text_render_literally(
        self, stub_client, step
    ) -> None:
        resp = QueryResponse(
            query="q",
            answer="ok",
            citations=[
                CitationResponse(
                    document_id="d",
                    document_title=self.HOSTILE,
                    chunk_id="c",
                    content_snippet=self.HOSTILE,
                )
            ],
        )
        stub_client.ask_script = [step(resp)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        # Wide terminal: at the default 80 columns the preview wraps the string
        # mid-way, which would make the substring check below meaningless.
        async with app.run_test(size=(200, 60)) as pilot:
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert app.is_running
            assert self.HOSTILE in _pane(app).preview_text
            visible = screen_text(app)
            # Appears three times: the answer box is "ok", so these are the
            # list row, the preview title, and the preview body.
            assert visible.count("[bold red]boom[/]") >= 2
            assert "[link=http://evil.example]click[/link]" in visible

    async def test_a_hostile_answer_does_not_crash_or_get_parsed_as_markup(
        self, stub_client, step
    ) -> None:
        stub_client.ask_script = [step(_answer(self.HOSTILE))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(200, 60)) as pilot:
            await _submit(app, pilot)
            await _settled(app, pilot)

            assert app.is_running
            visible = screen_text(app)
            assert "[bold red]boom[/]" in visible
            assert "[link=http://evil.example]click[/link]" in visible

    async def test_links_in_an_answer_are_never_opened_automatically(
        self, stub_client, step
    ) -> None:
        stub_client.ask_script = [step(_answer("[click](http://evil.example)"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        opened: list[str] = []
        app.open_url = lambda url, **kw: opened.append(url)  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            await _submit(app, pilot)
            await _settled(app, pilot)
            markdown = app.query_one("#answer", Markdown)

            markdown.post_message(Markdown.LinkClicked(markdown, "http://evil.example"))
            await pilot.pause()

            assert opened == []


class TestFocus:
    async def test_the_query_box_has_focus_on_launch(self, stub_client) -> None:
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await pilot.pause()

            assert app.focused is app.query_one("#query-input")

    async def test_returning_to_the_tab_refocuses_the_query_box(
        self, stub_client
    ) -> None:
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await pilot.pause()  # let the startup focus (tab activation) settle
            app.query_one("#top-k", Input).focus()  # focus is somewhere else
            await pilot.pause()
            assert app.focused is not app.query_one("#query-input")

            await pilot.press("f2")
            await pilot.pause()
            await pilot.press("f1")
            await pilot.pause()

            assert app.focused is app.query_one("#query-input")


class TestWithTheRealClient:
    """Through httpx, pydantic and a real socket: no stubbed client."""

    @staticmethod
    def _serve(recorded: list[dict[str, Any]]) -> tuple[Any, int]:
        import json
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                self.send_response(200)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802 - http.server's naming
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                recorded.append(
                    {
                        "path": self.path,
                        "body": body,
                        "api_key": self.headers.get("X-API-Key"),
                        "session": self.headers.get("X-Session-Id"),
                    }
                )
                if self.path.endswith("/ask"):
                    payload = _answer("Real answer.", n=1).model_dump()
                else:
                    payload = _found(n=1).model_dump()
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, server.server_address[1]

    async def test_ask_and_search_round_trip_with_filters_and_headers(self) -> None:
        from grimoire.gui.client import GrimoireClient
        from grimoire.gui.config import GuiConfig

        recorded: list[dict[str, Any]] = []
        server, port = self._serve(recorded)
        config = GuiConfig(
            base_url=f"http://127.0.0.1:{port}",
            api_key="grim_agt_real",
            session_id="sess-e2e",
        )
        client = GrimoireClient(config)
        try:
            app = GrimoireApp(client, config, None)
            async with app.run_test() as pilot:
                app.query_one("#severity", Select).value = "high"
                app.query_one("#tags-input", Input).value = "x, y"
                app.query_one("#top-k", Input).value = "7"
                await _submit(app, pilot, "what is sigma")
                await _settled(app, pilot)

                assert _pane(app).answer_text == "Real answer."
                assert [s.title for s in _pane(app).sources] == ["Doc 0"]

                app.query_one("#mode-search").value = True  # type: ignore[attr-defined]
                await pilot.pause()
                await _submit(app, pilot, "sigma")
                await _settled(app, pilot)

                assert [s.title for s in _pane(app).sources] == ["Found 0"]
        finally:
            client.close()
            server.shutdown()

        ask, search = recorded
        assert ask["path"].endswith("/query/ask")
        assert ask["body"]["query"] == "what is sigma"
        assert ask["body"]["top_k"] == 7
        assert ask["body"]["filter_dict"] == {"tags": ["x", "y"], "severity": "high"}
        assert search["path"].endswith("/query/search")
        for request in (ask, search):
            assert request["api_key"] == "grim_agt_real"
            assert request["session"] == "sess-e2e"
