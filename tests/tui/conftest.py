"""Shared fixtures for the TUI tests.

``StubClient`` stands in for ``GrimoireClient``.  It is plain Python (no
Textual), so this module imports without the ``tui`` extra installed.  Calls
are recorded, and each can be scripted to block on an event or to raise, which
is how the tests exercise in-flight and failure behaviour deterministically.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from grimoire.gui.config import GuiConfig


@dataclass
class _Step:
    """One scripted outcome for a client call."""

    result: Any = True
    gate: threading.Event | None = None  # block until set, to hold a call in flight


@dataclass
class StubClient:
    """Records calls and returns scripted outcomes, in order, then a default."""

    config: GuiConfig = field(
        default_factory=lambda: GuiConfig(
            base_url="http://stub:8001", api_key="grim_agt_test"
        )
    )
    healthy: bool = True
    health_script: list[_Step] = field(default_factory=list)
    health_calls: int = 0
    closed: int = 0
    ask_script: list[_Step] = field(default_factory=list)
    search_script: list[_Step] = field(default_factory=list)
    documents_script: list[_Step] = field(default_factory=list)
    detail_script: list[_Step] = field(default_factory=list)
    # Every ask/search call, in order: (method, positional args, keyword args).
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(
        default_factory=list
    )

    @staticmethod
    def _run(step: _Step) -> Any:
        if step.gate is not None:
            step.gate.wait(timeout=5)  # bounded so a broken test cannot hang CI
        if isinstance(step.result, BaseException):
            raise step.result
        return step.result

    def health(self) -> bool:
        self.health_calls += 1
        step = self.health_script.pop(0) if self.health_script else _Step(self.healthy)
        return bool(self._run(step))

    def ask(self, query: str, **kwargs: Any) -> Any:
        self.calls.append(("ask", (query,), kwargs))
        return self._run(self.ask_script.pop(0))

    def search(self, query: str, **kwargs: Any) -> Any:
        self.calls.append(("search", (query,), kwargs))
        return self._run(self.search_script.pop(0))

    def list_documents(self, **kwargs: Any) -> Any:
        self.calls.append(("list_documents", (), kwargs))
        return self._run(self.documents_script.pop(0))

    def get_document(self, document_id: str) -> Any:
        self.calls.append(("get_document", (document_id,), {}))
        return self._run(self.detail_script.pop(0))

    def calls_to(self, method: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        return [(a, k) for m, a, k in self.calls if m == method]

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def step() -> type[_Step]:
    """Factory for scripted outcomes: ``step(False, gate=event)``."""
    return _Step


@pytest.fixture
def stub_client() -> StubClient:
    return StubClient()


def screen_text(app: Any) -> str:
    """The text currently visible on screen, one line per terminal row.

    Rebuilt from Textual's SVG export, which escapes characters such as ``[``
    and splits each row into per-style runs, so a plain substring search of the
    raw SVG cannot see what a person would read.
    """
    import html
    import re

    svg: str = app.export_screenshot()
    rows: dict[float, list[tuple[float, str]]] = {}
    for match in re.finditer(
        r'<text[^>]*x="([\d.]+)" y="([\d.]+)"[^>]*>(.*?)</text>', svg
    ):
        x, y, raw = float(match.group(1)), float(match.group(2)), match.group(3)
        rows.setdefault(y, []).append((x, html.unescape(raw)))
    lines = (
        "".join(text for _, text in sorted(rows[y])).replace("\xa0", " ").rstrip()
        for y in sorted(rows)
    )
    return "\n".join(line for line in lines if line.strip())
