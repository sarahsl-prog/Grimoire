"""Adding and removing tags from the document detail view.

``a`` offers the categories the document lacks, ``x`` the ones it has. The
client is a stub, so the tests can see exactly which calls were made, hold one
in flight, or make it raise (a read-tier key is refused by the server).
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from textual.widgets import OptionList  # noqa: E402

from grimoire.api.schemas import DocumentDetailResponse  # noqa: E402
from grimoire.client.errors import ConnectionFailed, RequestRejected  # noqa: E402
from grimoire.tui.app import GrimoireApp  # noqa: E402
from grimoire.tui.screens.document_detail import DocumentDetailScreen  # noqa: E402
from grimoire.tui.screens.tag_picker import TagPickerScreen  # noqa: E402

from .test_categories_pane import _cat, _list  # noqa: E402
from .test_documents_pane import _open, _page, _settled, _until  # noqa: E402


def _detail(*tags: Any) -> DocumentDetailResponse:
    return DocumentDetailResponse(
        id="doc-0",
        title="Document 0",
        source_path="/p/x.pdf",
        file_type="pdf",
        storage_backend="local",
        processing_status="completed",
        tags=[c.name for c in tags],
        categories=list(tags),
        tag_count=len(tags),
    )


async def _open_detail(app: GrimoireApp, pilot: Any) -> DocumentDetailScreen:
    """Documents tab, first row, Enter, and wait for the detail to load."""
    await _open(app, pilot)
    await pilot.press("enter")
    await _until(pilot, lambda: isinstance(app.screen, DocumentDetailScreen))
    screen = app.screen
    assert isinstance(screen, DocumentDetailScreen)
    await _until(pilot, lambda: screen.status_text == "")
    return screen


async def _picker(app: GrimoireApp, pilot: Any, key: str) -> TagPickerScreen:
    await pilot.press(key)
    await _until(pilot, lambda: isinstance(app.screen, TagPickerScreen))
    picker = app.screen
    assert isinstance(picker, TagPickerScreen)
    return picker


def _options(picker: TagPickerScreen) -> list[str]:
    option_list = picker.query_one("#picker-list", OptionList)
    return [
        str(option_list.get_option_at_index(i).prompt)
        for i in range(option_list.option_count)
    ]


class TestDiscoverability:
    async def test_the_detail_names_the_tag_keys(self, stub_client, step) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _open_detail(app, pilot)

            hint = str(screen.query_one("#detail-hint").render())
            assert "a add tag" in hint
            assert "x remove tag" in hint


class TestAdd:
    async def test_offers_only_the_categories_the_document_lacks(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail(_cat(1)))]
        stub_client.categories_script = [step(_list(_cat(1), _cat(2), _cat(3)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open_detail(app, pilot)
            picker = await _picker(app, pilot, "a")
            await _until(pilot, lambda: len(_options(picker)) == 2)

            assert _options(picker) == ["Category 2", "Category 3"]

    async def test_choosing_one_tags_the_document_and_shows_the_result(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1)), step(_page(1))]
        stub_client.detail_script = [step(_detail()), step(_detail(_cat(2)))]
        stub_client.categories_script = [step(_list(_cat(1), _cat(2)))]
        stub_client.tag_script = [step(None)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _open_detail(app, pilot)
            picker = await _picker(app, pilot, "a")
            await _until(pilot, lambda: len(_options(picker)) == 2)
            await pilot.press("down", "enter")  # the second option: Category 2
            await _until(pilot, lambda: screen.values["tags"] == "Category 2")

        assert stub_client.calls_to("tag_document") == [(("doc-0", "cat-2"), {})]
        assert len(stub_client.calls_to("get_document")) == 2  # reloaded after

    async def test_a_document_with_every_category_says_so(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail(_cat(1)))]
        stub_client.categories_script = [step(_list(_cat(1)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open_detail(app, pilot)
            picker = await _picker(app, pilot, "a")
            await _until(pilot, lambda: "already has every" in picker.status_text)

            assert _options(picker) == []

    async def test_a_failed_category_fetch_is_shown_in_the_picker(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail())]
        stub_client.categories_script = [
            step(ConnectionFailed("Cannot reach the API."))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open_detail(app, pilot)
            picker = await _picker(app, pilot, "a")
            await _until(pilot, lambda: "Cannot reach" in picker.status_text)

    async def test_escape_in_the_picker_changes_nothing(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail())]
        stub_client.categories_script = [step(_list(_cat(1)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _open_detail(app, pilot)
            await _picker(app, pilot, "a")
            await pilot.press("escape")
            await _until(pilot, lambda: app.screen is screen)

        assert stub_client.calls_to("tag_document") == []

    async def test_a_refused_tag_is_explained_in_the_detail(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail())]
        stub_client.categories_script = [step(_list(_cat(1)))]
        stub_client.tag_script = [
            step(
                RequestRejected(
                    "This API key is not permitted to do that: requires tier 'dvl'"
                )
            )
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _open_detail(app, pilot)
            picker = await _picker(app, pilot, "a")
            await _until(pilot, lambda: len(_options(picker)) == 1)
            await pilot.press("enter")
            await _until(pilot, lambda: "requires tier" in screen.status_text)

            assert app.screen is screen  # still open, message readable


class TestRemove:
    async def test_offers_the_documents_own_tags_without_asking_the_server(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail(_cat(1), _cat(2)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open_detail(app, pilot)
            picker = await _picker(app, pilot, "x")
            await _settled(app, pilot)

            assert _options(picker) == ["Category 1", "Category 2"]
        assert stub_client.calls_to("list_categories") == []

    async def test_choosing_one_untags_the_document(self, stub_client, step) -> None:
        stub_client.documents_script = [step(_page(1)), step(_page(1))]
        stub_client.detail_script = [
            step(_detail(_cat(1), _cat(2))),
            step(_detail(_cat(1))),
        ]
        stub_client.tag_script = [step(None)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _open_detail(app, pilot)
            await _picker(app, pilot, "x")
            await pilot.press("down", "enter")  # Category 2
            await _until(pilot, lambda: screen.values["tags"] == "Category 1")

        assert stub_client.calls_to("untag_document") == [(("doc-0", "cat-2"), {})]

    async def test_with_no_tags_it_says_so_and_opens_nothing(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _open_detail(app, pilot)
            await pilot.press("x")
            await _until(pilot, lambda: "no tags" in screen.status_text)

            assert app.screen is screen


class TestUntrustedText:
    async def test_markup_in_a_category_name_is_shown_literally(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail(_cat(1, name="[bold red]x[/]")))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open_detail(app, pilot)
            picker = await _picker(app, pilot, "x")
            await _settled(app, pilot)

            assert _options(picker) == ["[bold red]x[/]"]


class TestRefreshingTheDocumentsTable:
    async def test_closing_after_a_change_reloads_the_page(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1)), step(_page(1))]
        stub_client.detail_script = [step(_detail()), step(_detail(_cat(1)))]
        stub_client.categories_script = [step(_list(_cat(1)))]
        stub_client.tag_script = [step(None)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            screen = await _open_detail(app, pilot)
            picker = await _picker(app, pilot, "a")
            await _until(pilot, lambda: len(_options(picker)) == 1)
            await pilot.press("enter")
            await _until(pilot, lambda: screen.values["tags"] == "Category 1")
            await pilot.press("escape")  # close the detail
            await _until(
                pilot, lambda: len(stub_client.calls_to("list_documents")) == 2
            )

    async def test_closing_without_a_change_does_not_reload(
        self, stub_client, step
    ) -> None:
        stub_client.documents_script = [step(_page(1))]
        stub_client.detail_script = [step(_detail())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open_detail(app, pilot)
            await pilot.press("escape")
            await _settled(app, pilot)
            for _ in range(6):
                await pilot.pause()

        assert len(stub_client.calls_to("list_documents")) == 1
