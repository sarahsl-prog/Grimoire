"""Tests for the Categories pane and the new-category modal.

As elsewhere, the client is a stub whose every call can be scripted, held in
flight, or made to raise. The risky properties are stale responses, duplicate
submits, and category names (untrusted text) rendering literally.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

pytest.importorskip("textual", reason="TUI extra not installed")

from textual.widgets import (
    Button,
    DataTable,
    Input,
    Select,
    TabbedContent,
)  # noqa: E402

from grimoire.api.schemas import CategoryListResponse, CategoryResponse  # noqa: E402
from grimoire.client.errors import ConnectionFailed, RequestRejected  # noqa: E402
from grimoire.tui.app import GrimoireApp  # noqa: E402
from grimoire.tui.errors import GENERIC_ERROR  # noqa: E402
from grimoire.tui.screens.new_category import NewCategoryScreen  # noqa: E402
from grimoire.tui.widgets.categories_pane import CategoriesPane  # noqa: E402

from .test_documents_pane import _launched, _settled, _until  # noqa: E402


def _cat(i: int, **overrides: Any) -> CategoryResponse:
    fields: dict[str, Any] = {
        "id": f"cat-{i}",
        "name": f"Category {i}",
        "slug": f"category-{i}",
        "description": f"About {i}",
        "parent_id": None,
        "color": "#3498db",
        "document_count": i,
    }
    fields.update(overrides)
    return CategoryResponse(**fields)


def _list(*cats: CategoryResponse) -> CategoryListResponse:
    return CategoryListResponse(categories=list(cats), total=len(cats))


def _pane(app: GrimoireApp) -> CategoriesPane:
    return app.query_one(CategoriesPane)


def _table(app: GrimoireApp) -> DataTable[Any]:
    return app.query_one("#categories-table", DataTable)


async def _open(app: GrimoireApp, pilot: Any) -> None:
    """Show the Categories tab (which triggers the first load) and let it finish."""
    await _launched(app, pilot)
    await pilot.press("f3")
    await _until(pilot, lambda: app.query_one(TabbedContent).active == "categories")
    await _settled(app, pilot)


def _cells(app: GrimoireApp) -> list[list[str]]:
    table = _table(app)
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


class TestLoading:
    async def test_f3_shows_the_tab_and_loads_once(self, stub_client, step) -> None:
        stub_client.categories_script = [step(_list(_cat(1), _cat(2)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert len(stub_client.calls_to("list_categories")) == 1
            assert _table(app).row_count == 2

    async def test_a_row_shows_name_count_parent_and_description(
        self, stub_client, step
    ) -> None:
        stub_client.categories_script = [
            step(
                _list(_cat(1, name="Parent"), _cat(2, name="Child", parent_id="cat-1"))
            )
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(app, pilot)

            rows = _cells(app)
        assert rows[0][:3] == ["Parent", "1", "-"]
        assert rows[1][:3] == ["Child", "2", "Parent"]
        assert rows[1][3] == "About 2"

    async def test_no_categories_says_how_to_make_one(self, stub_client, step) -> None:
        stub_client.categories_script = [step(_list())]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert app.query_one("#categories-empty").display is True
            assert _table(app).display is False
            assert "Press n" in _pane(app).footer_text

    async def test_the_footer_counts_them(self, stub_client, step) -> None:
        stub_client.categories_script = [step(_list(_cat(1), _cat(2), _cat(3)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert _pane(app).footer_text == "3 categories"

    async def test_a_failed_load_is_shown_and_retried_on_the_next_visit(
        self, stub_client, step
    ) -> None:
        stub_client.categories_script = [
            step(ConnectionFailed("Cannot reach the API.")),
            step(_list(_cat(1))),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            assert "Cannot reach the API." in _pane(app).status_text

            await pilot.press("f1")
            await _settled(app, pilot)
            await pilot.press("f3")
            await _until(pilot, lambda: _table(app).row_count == 1)

    async def test_an_unexpected_error_shows_the_generic_line_not_its_text(
        self, stub_client, step
    ) -> None:
        stub_client.categories_script = [step(RuntimeError("SELECT secret FROM t"))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)

            assert _pane(app).status_text == GENERIC_ERROR

    async def test_a_late_response_for_an_older_request_is_dropped(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.categories_script = [
            step(_list(_cat(1, name="STALE")), gate=gate),
            step(_list(_cat(2, name="FRESH"))),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _launched(app, pilot)
            await pilot.press("f3")
            await _until(
                pilot, lambda: len(stub_client.calls_to("list_categories")) == 1
            )
            _pane(app).refresh_data()  # a second request, while the first is held
            await _until(
                pilot, lambda: len(stub_client.calls_to("list_categories")) == 2
            )
            gate.set()
            await _settled(app, pilot)

            assert [r[0] for r in _cells(app)] == ["FRESH"]


class TestUntrustedText:
    async def test_markup_in_a_name_is_shown_literally(self, stub_client, step) -> None:
        stub_client.categories_script = [step(_list(_cat(1, name="[bold red]x[/]")))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test(size=(120, 30)) as pilot:
            await _open(app, pilot)

            assert _cells(app)[0][0] == "[bold red]x[/]"


class TestRefresh:
    async def test_ctrl_r_reloads(self, stub_client, step) -> None:
        stub_client.categories_script = [
            step(_list(_cat(1))),
            step(_list(_cat(1), _cat(2))),
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await pilot.press("ctrl+r")
            await _until(pilot, lambda: _table(app).row_count == 2)


class TestNewCategory:
    async def _modal(self, app: GrimoireApp, pilot: Any) -> NewCategoryScreen:
        await pilot.press("n")
        await _until(pilot, lambda: isinstance(app.screen, NewCategoryScreen))
        return app.screen  # type: ignore[return-value]

    async def test_n_opens_the_modal_and_focuses_the_name(
        self, stub_client, step
    ) -> None:
        stub_client.categories_script = [step(_list(_cat(1)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)

            assert app.focused is screen.query_one("#newcat-name", Input)

    async def test_creating_sends_the_fields_and_reloads_the_list(
        self, stub_client, step
    ) -> None:
        stub_client.categories_script = [
            step(_list(_cat(1))),
            step(_list(_cat(1), _cat(2))),
        ]
        stub_client.create_category_script = [step(_cat(2))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            screen.query_one("#newcat-name", Input).value = "  Threat intel  "
            screen.query_one("#newcat-description", Input).value = "Feeds"
            screen.query_one("#newcat-parent", Select).value = "category-1"
            await pilot.click("#newcat-create")
            await _until(pilot, lambda: _table(app).row_count == 2)

            assert not isinstance(app.screen, NewCategoryScreen)
        ((args, kwargs),) = stub_client.calls_to("create_category")
        assert args == ("Threat intel",)
        assert kwargs == {"description": "Feeds", "parent_slug": "category-1"}

    async def test_no_parent_sends_none(self, stub_client, step) -> None:
        stub_client.categories_script = [step(_list()), step(_list(_cat(1)))]
        stub_client.create_category_script = [step(_cat(1))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            screen.query_one("#newcat-name", Input).value = "Solo"
            await pilot.press("enter")
            await _until(pilot, lambda: _table(app).row_count == 1)

        ((_, kwargs),) = stub_client.calls_to("create_category")
        assert kwargs == {"description": "", "parent_slug": None}

    async def test_a_blank_name_is_refused_without_calling_the_api(
        self, stub_client, step
    ) -> None:
        stub_client.categories_script = [step(_list(_cat(1)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            screen.query_one("#newcat-name", Input).value = "   "
            await pilot.press("enter")
            await pilot.pause()

            assert "Enter a name" in screen.error_text
            assert isinstance(app.screen, NewCategoryScreen)
        assert stub_client.calls_to("create_category") == []

    async def test_a_refused_create_stays_open_and_says_why(
        self, stub_client, step
    ) -> None:
        stub_client.categories_script = [step(_list(_cat(1)))]
        stub_client.create_category_script = [
            step(RequestRejected("This API key is not permitted to do that: needs dvl"))
        ]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            screen.query_one("#newcat-name", Input).value = "X"
            await pilot.press("enter")
            await _until(pilot, lambda: screen.error_text != "")

            assert "needs dvl" in screen.error_text
            assert isinstance(app.screen, NewCategoryScreen)
            assert screen.query_one("#newcat-create", Button).disabled is False

    async def test_a_second_enter_while_creating_does_not_create_twice(
        self, stub_client, step
    ) -> None:
        gate = threading.Event()
        stub_client.categories_script = [step(_list()), step(_list(_cat(1)))]
        stub_client.create_category_script = [step(_cat(1), gate=gate)]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            screen.query_one("#newcat-name", Input).value = "X"
            await pilot.press("enter")
            await _until(
                pilot, lambda: len(stub_client.calls_to("create_category")) == 1
            )
            await pilot.press("enter")
            await pilot.pause()
            gate.set()
            await _until(pilot, lambda: not isinstance(app.screen, NewCategoryScreen))

        assert len(stub_client.calls_to("create_category")) == 1

    async def test_escape_cancels_without_calling_the_api(
        self, stub_client, step
    ) -> None:
        stub_client.categories_script = [step(_list(_cat(1)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            await self._modal(app, pilot)
            await pilot.press("escape")
            await _until(pilot, lambda: not isinstance(app.screen, NewCategoryScreen))

        assert stub_client.calls_to("create_category") == []

    async def test_the_parent_choices_are_the_existing_categories(
        self, stub_client, step
    ) -> None:
        stub_client.categories_script = [step(_list(_cat(1), _cat(2)))]
        app = GrimoireApp(stub_client, stub_client.config, None)
        async with app.run_test() as pilot:
            await _open(app, pilot)
            screen = await self._modal(app, pilot)
            parent = screen.query_one("#newcat-parent", Select)

            assert parent.is_blank()
            # The blank "No parent" entry is a sentinel, not a string.
            assert [v for _, v in parent._options if isinstance(v, str)] == [
                "category-1",
                "category-2",
            ]
