"""The Documents table's width planning (B3).

Pure functions over numbers, so no Textual and no terminal: the fiddly cases
(a width that is too small for anything, a column that must never be dropped)
are tested directly.
"""

from __future__ import annotations

import pytest

from grimoire.tui.layout import (
    COLUMN_ORDER,
    DROP_ORDER,
    MAX_TITLE_WIDTH,
    MIN_TITLE_WIDTH,
    PAD,
    ColumnPlan,
    plan_columns,
)

# Natural content widths of a typical page, without padding.
NATURAL = {
    "title": 40,
    "type": 4,
    "status": 9,
    "chunks": 6,
    "tags": 4,
    "size": 6,
    "added": 16,
}


def _total(plan: ColumnPlan, natural: dict[str, int] = NATURAL) -> int:
    """Width the plan needs, with each column's padding."""
    widths = {**natural, "title": plan.title_width}
    return sum(widths[key] + PAD for key in plan.visible)


class TestFits:
    def test_everything_shows_when_there_is_room(self) -> None:
        plan = plan_columns(200, NATURAL)

        assert plan.visible == COLUMN_ORDER
        assert plan.title_width == 40
        assert plan.hidden == ()

    def test_exactly_enough_room_drops_nothing(self) -> None:
        need = sum(NATURAL[k] + PAD for k in COLUMN_ORDER)

        assert plan_columns(need, NATURAL).visible == COLUMN_ORDER
        assert plan_columns(need, NATURAL).title_width == 40
        # One character short: the title gives it up, not a column.
        short = plan_columns(need - 1, NATURAL)
        assert short.visible == COLUMN_ORDER
        assert short.title_width == 39

    def test_one_enormous_title_is_capped_however_wide_the_terminal(self) -> None:
        plan = plan_columns(1000, {**NATURAL, "title": 5000})

        assert plan.title_width == MAX_TITLE_WIDTH == 80

    def test_the_title_is_not_padded_beyond_its_content(self) -> None:
        plan = plan_columns(200, {**NATURAL, "title": 12})

        assert plan.title_width == 12


class TestShrinkingTheTitleFirst:
    def test_a_long_title_shrinks_before_any_column_is_dropped(self) -> None:
        natural = {**NATURAL, "title": 80}
        plan = plan_columns(100, natural)

        assert plan.visible == COLUMN_ORDER
        assert MIN_TITLE_WIDTH <= plan.title_width < 80
        assert _total(plan, natural) <= 100

    def test_the_title_takes_whatever_room_is_left(self) -> None:
        natural = {**NATURAL, "title": 200}
        plan = plan_columns(120, natural)

        assert _total(plan, natural) == 120  # no space wasted, none overflowing


class TestDroppingColumns:
    def test_columns_go_in_the_documented_order(self) -> None:
        seen: list[tuple[str, ...]] = []
        for width in range(140, 30, -1):
            plan = plan_columns(width, NATURAL)
            if not seen or seen[-1] != plan.hidden:
                seen.append(plan.hidden)

        # Each step hides exactly one more column, in DROP_ORDER.
        for earlier, later in zip(seen, seen[1:], strict=False):
            assert later[: len(earlier)] == earlier
            assert len(later) >= len(earlier)
        flat = [c for hidden in seen for c in hidden]
        assert [c for c in DROP_ORDER if c in flat] == list(dict.fromkeys(flat))

    def test_the_order_of_giving_up_is_tags_chunks_type_size(self) -> None:
        """Pinned as behaviour (the first column to go as the width shrinks, and
        so on), not just by reading the constant back."""
        order: list[str] = []
        for width in range(200, 30, -1):
            for column in plan_columns(width, {**NATURAL, "title": 120}).hidden:
                if column not in order:
                    order.append(column)

        assert order == ["tags", "chunks", "type", "size"]

    @pytest.mark.parametrize("width", range(30, 200, 7))
    def test_title_status_and_added_are_never_dropped(self, width: int) -> None:
        plan = plan_columns(width, NATURAL)

        assert {"title", "status", "added"} <= set(plan.visible)

    @pytest.mark.parametrize("width", range(55, 200, 5))
    def test_it_always_fits_once_there_is_room_for_the_essentials(
        self, width: int
    ) -> None:
        plan = plan_columns(width, {**NATURAL, "title": 120})

        assert _total(plan, {**NATURAL, "title": 120}) <= width

    def test_the_visible_columns_keep_their_order(self) -> None:
        plan = plan_columns(70, NATURAL)

        assert list(plan.visible) == [k for k in COLUMN_ORDER if k in plan.visible]

    def test_hidden_lists_what_was_dropped(self) -> None:
        plan = plan_columns(70, NATURAL)

        assert set(plan.hidden) == set(COLUMN_ORDER) - set(plan.visible)
        assert plan.hidden  # at 70 something had to go


class TestHopelesslyNarrow:
    def test_it_keeps_the_essentials_at_minimum_title_width(self) -> None:
        plan = plan_columns(10, NATURAL)

        assert set(plan.visible) == {"title", "status", "added"}
        assert plan.title_width == MIN_TITLE_WIDTH

    def test_a_zero_or_negative_width_means_not_laid_out_yet(self) -> None:
        """Before the first layout the width is 0; show everything, not nothing."""
        for width in (0, -5):
            plan = plan_columns(width, NATURAL)

            assert plan.visible == COLUMN_ORDER
            assert plan.title_width == 40


class TestInputs:
    def test_a_missing_natural_width_falls_back_to_the_label_width(self) -> None:
        plan = plan_columns(200, {"title": 10})

        assert plan.visible == COLUMN_ORDER

    def test_title_width_never_goes_below_the_minimum_even_with_an_empty_page(
        self,
    ) -> None:
        plan = plan_columns(200, {**NATURAL, "title": 0})

        assert plan.title_width >= 5  # at least the "Title" label
