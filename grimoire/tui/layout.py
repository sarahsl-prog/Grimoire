"""Width planning for the Documents table.

Pure arithmetic, no Textual: given how wide the table is and how wide each
column's content naturally is, decide which columns to show and how wide the
title may be.  A terminal can be 80 columns or fewer, and a table that scrolls
sideways hides the Added column (the one you scan for) on every row.

The order of give-ups is deliberate.  The title shrinks first, down to a floor,
because a clipped title is still recognisable.  Only then do whole columns go,
least valuable first.  Title, Status and Added are never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Left to right, as shown.
COLUMN_ORDER: tuple[str, ...] = (
    "title",
    "type",
    "status",
    "chunks",
    "tags",
    "size",
    "added",
)

LABELS: dict[str, str] = {
    "title": "Title",
    "type": "Type",
    "status": "Status",
    "chunks": "Chunks",
    "tags": "Tags",
    "size": "Size",
    "added": "Added",
}

#: Columns given up, in this order, once the title cannot shrink any further.
DROP_ORDER: tuple[str, ...] = ("tags", "chunks", "type", "size")

#: A DataTable cell has one column of padding on each side.
PAD = 2

#: A title is cut to this at most, however wide the terminal is: one very long
#: title must not widen the whole column.
MAX_TITLE_WIDTH = 80

#: The narrowest a title is squeezed to before columns start going instead.
#: Below about this a title stops being recognisable ("a very long docu…"), and
#: a count column is worth less than being able to tell documents apart.
MIN_TITLE_WIDTH = 24


@dataclass(frozen=True)
class ColumnPlan:
    """Which columns to show, and how many characters of title.

    Attributes:
        visible: Column keys to show, in display order.
        title_width: Characters of title to keep (the cell is cut to this).
        widths: Content width of each visible column, in the same order, without
            padding.  The table is given these explicitly rather than left to
            measure its own cells: DataTable measures lazily, after the first
            paint, and under load a frame drawn before that can stay on screen
            with every cell clipped to its header's width.
    """

    visible: tuple[str, ...]
    title_width: int
    widths: tuple[int, ...]

    @property
    def hidden(self) -> tuple[str, ...]:
        """Columns left out, in the order they were given up."""
        return tuple(key for key in DROP_ORDER if key not in self.visible)


def plan_columns(available: int, natural: dict[str, int]) -> ColumnPlan:
    """Choose columns and title width for a table ``available`` characters wide.

    Args:
        available: Usable width of the table.  Zero or negative means "not laid
            out yet": everything is shown, since hiding columns on a guess would
            flash the wrong layout.
        natural: Width of each column's longest content, without padding.  A
            missing column is taken to be as wide as its label.

    Returns:
        A plan that fits ``available`` whenever the essential columns can.
    """
    widths = {
        key: max(natural.get(key, 0), len(label)) for key, label in LABELS.items()
    }
    title_natural = min(widths["title"], MAX_TITLE_WIDTH)
    if available <= 0:
        return _plan(COLUMN_ORDER, title_natural, widths)

    floor = min(title_natural, MIN_TITLE_WIDTH)
    visible = list(COLUMN_ORDER)
    give_up = iter(DROP_ORDER)
    while True:
        others = sum(widths[key] + PAD for key in visible if key != "title")
        title_width = min(title_natural, available - others - PAD)
        if title_width >= floor:
            return _plan(tuple(visible), title_width, widths)
        column = next(give_up, None)
        if column is None:
            # Nothing left to give up: keep the essentials and accept a scroll.
            return _plan(tuple(visible), floor, widths)
        visible.remove(column)


def _plan(
    visible: tuple[str, ...], title_width: int, widths: dict[str, int]
) -> ColumnPlan:
    """Build a plan, giving the title column its chosen width."""
    sized = {**widths, "title": title_width}
    return ColumnPlan(visible, title_width, tuple(sized[key] for key in visible))
