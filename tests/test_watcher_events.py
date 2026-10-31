"""Watcher event handling: moves, delete races, and lifecycle edges.

Covers audit findings C-6 and C-10:

* MOVED events used to be ignored, so a rename left a stale document at the
  old path and an editor's atomic save (temp file renamed over the original)
  was never re-ingested.
* A delete-then-recreate save could remove the document for a file that was
  back on disk.
* Events from the observer thread could hit a closed or missing event loop,
  and a crashed processor task was never restarted.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from grimoire.agents.watcher import WatcherAgent, _WatchTracker
from grimoire.storage.base import FileChange, FileChangeType
from grimoire.storage.watch_manager import WatchManager


@pytest.fixture
def watcher() -> WatcherAgent:
    manager = MagicMock(spec=WatchManager)
    manager.start_watch = AsyncMock(return_value="local:/w")
    manager.stop_all = AsyncMock()
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
    session_cm.__aexit__ = AsyncMock(return_value=None)
    return WatcherAgent(
        watch_manager=manager,
        ingestion_agent=MagicMock(),
        db_session_factory=MagicMock(return_value=session_cm),
    )


def _moved(src: str, dest: str) -> FileChange:
    return FileChange(change_type=FileChangeType.MOVED, path=dest, previous_path=src)


def _drain(watcher: WatcherAgent) -> list[tuple[FileChangeType, str]]:
    out = []
    while not watcher._processing_queue.empty():
        _, change = watcher._processing_queue.get_nowait()
        out.append((change.change_type, change.path))
    return out


class TestExpandChange:
    def test_move_becomes_delete_plus_create(self) -> None:
        events = WatcherAgent._expand_change(_moved("/w/a.pdf", "/w/b.pdf"))
        assert [(e.change_type, e.path) for e in events] == [
            (FileChangeType.DELETED, "/w/a.pdf"),
            (FileChangeType.CREATED, "/w/b.pdf"),
        ]

    def test_other_events_pass_through(self) -> None:
        change = FileChange(change_type=FileChangeType.MODIFIED, path="/w/a.pdf")
        assert WatcherAgent._expand_change(change) == [change]

    def test_move_without_source_passes_through(self) -> None:
        change = FileChange(change_type=FileChangeType.MOVED, path="/w/a.pdf")
        assert WatcherAgent._expand_change(change) == [change]


class TestCallbackQueueing:
    """on_change runs on the observer thread and must hand off to the loop."""

    async def _fire(self, watcher: WatcherAgent, change: FileChange) -> None:
        watcher._trackers["local:/w"] = _WatchTracker("local:/w", "/w", "local")
        watcher._event_loop = asyncio.get_running_loop()
        watcher._create_callback("/w")(change)
        await asyncio.sleep(0)  # let call_soon_threadsafe callbacks run

    async def test_rename_queues_delete_and_create(self, watcher: WatcherAgent) -> None:
        await self._fire(watcher, _moved("/w/a.pdf", "/w/b.pdf"))
        assert _drain(watcher) == [
            (FileChangeType.DELETED, "/w/a.pdf"),
            (FileChangeType.CREATED, "/w/b.pdf"),
        ]

    async def test_atomic_save_reingests_target(self, watcher: WatcherAgent) -> None:
        """vim-style save: write notes.txt.swp, rename it over notes.txt."""
        await self._fire(watcher, _moved("/w/notes.txt.swp", "/w/notes.txt"))
        # The temp file's DELETED half is filtered out (unsupported suffix).
        assert _drain(watcher) == [(FileChangeType.CREATED, "/w/notes.txt")]

    async def test_events_after_stop_are_dropped(self, watcher: WatcherAgent) -> None:
        watcher._trackers["local:/w"] = _WatchTracker("local:/w", "/w", "local")
        watcher._event_loop = None  # what stop_all leaves behind
        watcher._create_callback("/w")(
            FileChange(change_type=FileChangeType.CREATED, path="/w/a.pdf")
        )
        assert _drain(watcher) == []

    def test_closed_loop_does_not_raise(self, watcher: WatcherAgent) -> None:
        loop = asyncio.new_event_loop()
        loop.close()
        watcher._trackers["local:/w"] = _WatchTracker("local:/w", "/w", "local")
        watcher._event_loop = loop
        # Must not propagate RuntimeError into the observer thread.
        watcher._create_callback("/w")(
            FileChange(change_type=FileChangeType.CREATED, path="/w/a.pdf")
        )


class TestProcessorLifecycle:
    async def test_dead_processor_is_restarted(self, watcher: WatcherAgent) -> None:
        watcher._start_processor()
        first = watcher._processor_task
        assert first is not None
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        # _running is still True, as after an unexpected processor crash.
        assert watcher._running
        watcher._start_processor()
        assert watcher._processor_task is not first
        assert not watcher._processor_task.done()  # type: ignore[union-attr]
        await watcher.stop_all()

    async def test_stop_all_clears_loop_and_task(self, watcher: WatcherAgent) -> None:
        watcher._start_processor()
        await watcher.stop_all()
        assert watcher._event_loop is None
        assert watcher._processor_task is None
        assert not watcher._running
