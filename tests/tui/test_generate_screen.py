"""Tests for the Generate modal, opened with ``g`` from a document's detail view.

The client is a stub whose ``generate`` can be held in flight or made to raise.
The risky properties: a second press while generating must not send a second
request, an extract must not go out without its question, and generated text
(derived from untrusted documents) must render literally and not without bound.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from textual.widgets import Button, Input, Select  # noqa: E402

from grimoire.api.schemas import GenerateResponse  # noqa: E402
from grimoire.client.errors import RequestRejected  # noqa: E402
from grimoire.tui.app import GrimoireApp  # noqa: E402
from grimoire.tui.errors import GENERIC_ERROR  # noqa: E402
from grimoire.tui.screens.document_detail import DocumentDetailScreen  # noqa: E402
from grimoire.tui.screens.generate import GenerateScreen, describe  # noqa: E402

from .test_documents_pane import _page, _until  # noqa: E402
from .test_tag_editing import _detail, _open_detail  # noqa: E402


def _generated(content: str = "A short summary.", **fields: Any) -> GenerateResponse:
    base: dict[str, Any] = {
        "content": content,
        "content_type": "summary",
        "document_ids": ["doc-0"],
        "model_used": "llama3.2",
        "cached": False,
        "duration_ms": 4200,
    }
    base.update(fields)
    return GenerateResponse(**base)


async def _open_generate(app: GrimoireApp, pilot: Any) -> GenerateScreen:
    await _open_detail(app, pilot)
    await pilot.press("g")
    await _until(pilot, lambda: isinstance(app.screen, GenerateScreen))
    screen = app.screen
    assert isinstance(screen, GenerateScreen)
    return screen


def _setup(stub_client: Any, step: Any, *scripted: Any) -> GrimoireApp:
    stub_client.documents_script = [step(_page(1))]
    stub_client.detail_script = [step(_detail())]
    stub_client.generate_script = list(scripted)
    return GrimoireApp(stub_client, stub_client.config, None)


class TestDescribe:
    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({}, "llama3.2 · 4.2s"),
            ({"cached": True}, "llama3.2 · 4.2s · cached"),
            ({"model_used": ""}, "4.2s"),
            ({"model_used": "", "duration_ms": 0}, ""),
        ],
    )
    def test_one_line_about_how_it_was_made(self, fields: dict, expected: str) -> None:
        assert describe(_generated(**fields)) == expected


class TestOpening:
    async def test_g_opens_it_with_the_documents_title(self, stub_client, step) -> None:
        app = _setup(stub_client, step)
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)

            assert "Document 0" in str(screen.query_one("#gen-heading").render())
            assert stub_client.calls_to("generate") == []  # nothing until asked

    async def test_the_detail_hint_names_the_key(self, stub_client, step) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _open_detail(app, pilot)

            assert "g generate" in str(screen.query_one("#detail-hint").render())

    async def test_escape_returns_to_the_detail(self, stub_client, step) -> None:
        app = _setup(stub_client, step)
        async with app.run_test() as pilot:
            await _open_generate(app, pilot)
            await pilot.press("escape")
            await _until(pilot, lambda: isinstance(app.screen, DocumentDetailScreen))


class TestGenerating:
    async def test_a_summary_is_requested_and_shown(self, stub_client, step) -> None:
        app = _setup(stub_client, step, step(_generated()))
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            await pilot.press("enter")  # the Generate button has focus
            await _until(pilot, lambda: screen.output_text != "")

            assert screen.output_text == "A short summary."
            assert screen.meta_text == "llama3.2 · 4.2s"
            assert screen.status_text == ""
        ((args, kwargs),) = stub_client.calls_to("generate")
        assert args == (["doc-0"], "summary")
        assert kwargs == {"query": None}

    @pytest.mark.parametrize(
        ("value", "sent"),
        [
            ("flash_card", "flash_card"),
            ("cliff_notes", "cliff_notes"),
            ("outline", "outline"),
        ],
    )
    async def test_the_chosen_kind_is_what_is_sent(
        self, stub_client, step, value: str, sent: str
    ) -> None:
        app = _setup(stub_client, step, step(_generated()))
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            screen.query_one("#gen-kind", Select).value = value
            await pilot.press("enter")
            await _until(pilot, lambda: screen.output_text != "")

        assert stub_client.calls_to("generate")[0][0][1] == sent

    async def test_a_cached_result_says_so(self, stub_client, step) -> None:
        app = _setup(stub_client, step, step(_generated(cached=True)))
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            await pilot.press("enter")
            await _until(pilot, lambda: screen.output_text != "")

            assert screen.meta_text.endswith("cached")

    async def test_a_second_press_while_generating_sends_nothing_more(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        app = _setup(stub_client, step, step(_generated(), gate=gate))
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            await pilot.press("enter")
            await _until(pilot, lambda: len(stub_client.calls_to("generate")) == 1)
            assert screen.query_one("#gen-go", Button).disabled is True
            assert "Generating" in screen.status_text
            await pilot.press("enter")
            await pilot.pause()
            gate.set()
            await _until(pilot, lambda: screen.output_text != "")

        assert len(stub_client.calls_to("generate")) == 1

    async def test_it_can_be_used_again_after_a_result(self, stub_client, step) -> None:
        app = _setup(
            stub_client, step, step(_generated("one")), step(_generated("two"))
        )
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            await pilot.press("enter")
            await _until(pilot, lambda: screen.output_text == "one")
            screen.query_one("#gen-go", Button).focus()
            # Textual ignores a second press of the same button within its 0.2 s
            # "active" effect; a person cannot press that fast, a test can.
            await pilot.pause(0.3)
            await pilot.press("enter")
            await _until(pilot, lambda: screen.output_text == "two")

        assert len(stub_client.calls_to("generate")) == 2


class TestExtract:
    async def test_the_question_box_appears_only_for_an_extract(
        self, stub_client, step
    ) -> None:
        app = _setup(stub_client, step)
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            query = screen.query_one("#gen-query", Input)
            assert query.display is False

            screen.query_one("#gen-kind", Select).value = "extract"
            await pilot.pause()
            assert query.display is True

            screen.query_one("#gen-kind", Select).value = "summary"
            await pilot.pause()
            assert query.display is False

    async def test_an_extract_without_a_question_is_refused_locally(
        self, stub_client, step
    ) -> None:
        app = _setup(stub_client, step)
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            screen.query_one("#gen-kind", Select).value = "extract"
            await pilot.pause()
            screen.query_one("#gen-query", Input).value = "   "
            await pilot.press("enter")  # on the Generate button
            await pilot.pause()

            assert "Say what to extract" in screen.status_text
        assert stub_client.calls_to("generate") == []

    async def test_the_question_is_sent_trimmed(self, stub_client, step) -> None:
        app = _setup(stub_client, step, step(_generated()))
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            screen.query_one("#gen-kind", Select).value = "extract"
            await pilot.pause()
            box = screen.query_one("#gen-query", Input)
            box.focus()
            box.value = "  which CVE ids?  "
            await pilot.press("enter")  # Enter in the box submits
            await _until(pilot, lambda: screen.output_text != "")

        ((args, kwargs),) = stub_client.calls_to("generate")
        assert args[1] == "extract"
        assert kwargs == {"query": "which CVE ids?"}

    async def test_a_question_left_in_the_box_is_not_sent_for_other_kinds(
        self, stub_client, step
    ) -> None:
        app = _setup(stub_client, step, step(_generated()))
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            screen.query_one("#gen-query", Input).value = "leftover"
            await pilot.press("enter")
            await _until(pilot, lambda: screen.output_text != "")

        assert stub_client.calls_to("generate")[0][1] == {"query": None}


class TestFailures:
    async def test_a_refusal_is_explained_and_it_can_be_retried(
        self, stub_client, step
    ) -> None:
        app = _setup(
            stub_client,
            step,
            step(
                RequestRejected("This API key is not permitted to do that: needs dvl")
            ),
            step(_generated()),
        )
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            await pilot.press("enter")
            await _until(pilot, lambda: "needs dvl" in screen.status_text)
            assert screen.query_one("#gen-go", Button).disabled is False
            assert screen.output_text == ""
            assert app.focused is screen.query_one("#gen-go", Button)

            await pilot.pause(0.3)  # past the button's 0.2 s debounce
            await pilot.press("enter")
            await _until(pilot, lambda: screen.output_text != "")

    async def test_an_unexpected_error_shows_the_generic_line(
        self, stub_client, step
    ) -> None:
        app = _setup(stub_client, step, step(RuntimeError("Traceback /srv/secret")))
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            await pilot.press("enter")
            await _until(pilot, lambda: screen.status_text == GENERIC_ERROR)


class TestUntrustedOutput:
    async def test_markup_in_the_output_is_shown_literally(
        self, stub_client, step
    ) -> None:
        app = _setup(
            stub_client, step, step(_generated("[bold red]x[/] [link=http://e]y"))
        )
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            await pilot.press("enter")
            await _until(pilot, lambda: screen.output_text != "")

            assert screen.output_text == "[bold red]x[/] [link=http://e]y"
            # What is painted, not just what was stored: with markup parsing on,
            # the tags would be consumed and only "x y" would remain.
            assert str(screen.query_one("#gen-output").render()) == screen.output_text

    async def test_a_huge_output_is_capped(self, stub_client, step) -> None:
        app = _setup(stub_client, step, step(_generated("x" * 200_000)))
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            await pilot.press("enter")
            await _until(pilot, lambda: screen.output_text != "")

            assert len(screen.output_text) < 51_000
            assert screen.output_text.endswith("(Output truncated for display.)")

    @pytest.mark.parametrize("content", ["", "   \n  "])
    async def test_an_empty_result_says_so(
        self, stub_client, step, content: str
    ) -> None:
        app = _setup(stub_client, step, step(_generated(content)))
        async with app.run_test() as pilot:
            screen = await _open_generate(app, pilot)
            await pilot.press("enter")
            await _until(pilot, lambda: screen.output_text != "")

            assert screen.output_text == "(The model returned no text.)"


class TestClosingWhileGenerating:
    async def test_the_late_answer_is_discarded_without_harm(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        app = _setup(stub_client, step, step(_generated(), gate=gate))
        async with app.run_test() as pilot:
            await _open_generate(app, pilot)
            await pilot.press("enter")
            await _until(pilot, lambda: len(stub_client.calls_to("generate")) == 1)
            await pilot.press("escape")
            await _until(pilot, lambda: isinstance(app.screen, DocumentDetailScreen))
            gate.set()
            for _ in range(8):
                await pilot.pause()

            assert isinstance(app.screen, DocumentDetailScreen)
