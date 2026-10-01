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

    def health(self) -> bool:
        self.health_calls += 1
        step = self.health_script.pop(0) if self.health_script else _Step(self.healthy)
        if step.gate is not None:
            step.gate.wait(timeout=5)  # bounded so a broken test cannot hang CI
        if isinstance(step.result, BaseException):
            raise step.result
        return bool(step.result)

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def step() -> type[_Step]:
    """Factory for scripted outcomes: ``step(False, gate=event)``."""
    return _Step


@pytest.fixture
def stub_client() -> StubClient:
    return StubClient()
