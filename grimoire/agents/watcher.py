"""Watcher Agent for monitoring directories and triggering ingestion.

Provides a long-running daemon that monitors directories for file changes
and spawns ingestion tasks for new or modified files. Supports both local
filesystem watching (via watchdog) and cloud storage polling.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from grimoire.agents.ingestion import IngestionAgent
from grimoire.db.models import StorageBackend
from grimoire.db.repositories import DocumentRepository
from grimoire.storage.base import FileChange, FileChangeType
from grimoire.storage.watch_manager import WatchManager
from grimoire.vectorstore import ChromaDBStore

# =============================================================================
# Data Models
# =============================================================================


class WatchStatus(BaseModel):
    """Status of a watched path.

    Attributes:
        watch_id: Unique identifier for the watch.
        path: Watched path.
        backend: Storage backend type.
        is_running: Whether the watch is active.
        files_processed: Number of files processed by this watch.
        files_failed: Number of files that failed processing.
        last_event_at: Timestamp of the last file event.
    """

    watch_id: str
    path: str
    backend: str
    is_running: bool = True
    files_processed: int = 0
    files_failed: int = 0
    last_event_at: str | None = None


class WatcherStats(BaseModel):
    """Overall watcher statistics.

    Attributes:
        active_watches: Number of active watches.
        total_files_processed: Total files processed across all watches.
        total_files_failed: Total files that failed processing.
        watches: Per-watch status.
    """

    active_watches: int = 0
    total_files_processed: int = 0
    total_files_failed: int = 0
    watches: list[WatchStatus] = Field(default_factory=list)


# =============================================================================
# Internal tracking
# =============================================================================


@dataclass
class _WatchTracker:
    """Tracks statistics for a single watch."""

    watch_id: str
    path: str
    backend: str
    files_processed: int = 0
    files_failed: int = 0
    last_event_at: datetime | None = None


# =============================================================================
# Watcher Agent
# =============================================================================


class WatcherAgent:
    """Monitors directories for changes and triggers document ingestion.

    Wraps the WatchManager to coordinate file watching with the
    IngestionAgent, automatically processing new and modified files.

    Args:
        watch_manager: WatchManager instance for file watching.
        ingestion_agent: IngestionAgent for processing detected files.
        db_session_factory: Async callable that returns database sessions.
        auto_tag: Whether to auto-tag ingested documents.
        supported_extensions: File extensions to process (None = all supported).

    Example:
        ```python
        watcher = WatcherAgent(
            watch_manager=WatchManager(),
            ingestion_agent=ingestion_agent,
            db_session_factory=get_db_context,
        )
        watch_id = await watcher.watch("/path/to/docs", backend="local")
        # ... later
        await watcher.stop(watch_id)
        ```
    """

    def __init__(
        self,
        watch_manager: WatchManager,
        ingestion_agent: IngestionAgent,
        db_session_factory: Callable[..., Any],
        *,
        auto_tag: bool = True,
        supported_extensions: set[str] | None = None,
    ) -> None:
        self._watch_manager = watch_manager
        self._ingestion_agent = ingestion_agent
        self._db_session_factory = db_session_factory
        self._auto_tag = auto_tag
        self._supported_extensions = supported_extensions or {
            ".pdf",
            ".docx",
            ".doc",
            ".pptx",
            ".ppt",
            ".xlsx",
            ".xls",
            ".html",
            ".htm",
            ".md",
            ".txt",
            ".png",
            ".jpg",
            ".jpeg",
            ".tiff",
            ".tif",
        }
        self._trackers: dict[str, _WatchTracker] = {}
        self._processing_queue: asyncio.Queue[tuple[str, FileChange]] = asyncio.Queue()
        self._processor_task: asyncio.Task[None] | None = None
        self._running = False
        self._event_loop: asyncio.AbstractEventLoop | None = None

        logger.debug("WatcherAgent initialized")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def watch(
        self,
        path: str,
        *,
        backend: str | StorageBackend = StorageBackend.LOCAL,
        recursive: bool = True,
        poll_interval: int | None = None,
    ) -> str:
        """Start watching a directory for file changes.

        Args:
            path: Directory path to watch.
            backend: Storage backend type.
            recursive: Whether to watch subdirectories.
            poll_interval: Override default poll interval (cloud backends).

        Returns:
            Watch ID for managing this watch.

        Raises:
            ValueError: If path is invalid or already watched.
            RuntimeError: If the watch cannot be started.
        """
        if isinstance(backend, str):
            backend = StorageBackend(backend)

        # Start the background processor if not running
        if not self._running:
            self._start_processor()

        callback = self._create_callback(path)

        watch_id = await self._watch_manager.start_watch(
            path=path,
            backend=backend,
            callback=callback,
            recursive=recursive,
            poll_interval=poll_interval,
        )

        self._trackers[watch_id] = _WatchTracker(
            watch_id=watch_id,
            path=path,
            backend=backend.value if isinstance(backend, StorageBackend) else backend,
        )

        logger.info(f"WatcherAgent: started watch {watch_id}")
        return watch_id

    async def unwatch(self, watch_id: str) -> bool:
        """Stop watching a specific path.

        Args:
            watch_id: Watch ID returned by watch().

        Returns:
            True if the watch was stopped, False if not found.
        """
        result = await self._watch_manager.stop_watch(watch_id)
        if result:
            self._trackers.pop(watch_id, None)
            logger.info(f"WatcherAgent: stopped watch {watch_id}")
        return result

    async def stop_all(self) -> None:
        """Stop all watches and the background processor."""
        await self._watch_manager.stop_all()
        self._trackers.clear()

        if self._processor_task and not self._processor_task.done():
            self._running = False
            self._processor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._processor_task
            self._processor_task = None

        logger.info("WatcherAgent: all watches stopped")

    def get_status(self) -> WatcherStats:
        """Get current watcher statistics.

        Returns:
            WatcherStats with per-watch details.
        """
        watches = []
        total_processed = 0
        total_failed = 0

        for tracker in self._trackers.values():
            watches.append(
                WatchStatus(
                    watch_id=tracker.watch_id,
                    path=tracker.path,
                    backend=tracker.backend,
                    is_running=True,
                    files_processed=tracker.files_processed,
                    files_failed=tracker.files_failed,
                    last_event_at=(
                        tracker.last_event_at.isoformat()
                        if tracker.last_event_at
                        else None
                    ),
                )
            )
            total_processed += tracker.files_processed
            total_failed += tracker.files_failed

        return WatcherStats(
            active_watches=len(watches),
            total_files_processed=total_processed,
            total_files_failed=total_failed,
            watches=watches,
        )

    # -------------------------------------------------------------------------
    # Event Handling
    # -------------------------------------------------------------------------

    def _create_callback(self, watch_path: str) -> Callable[[FileChange], None]:
        """Create a callback for file change events.

        Args:
            watch_path: The watched path for identifying the tracker.

        Returns:
            Callback function for the watch manager.
        """

        def on_change(change: FileChange) -> None:
            # Called from the watchdog observer thread for local watches, so it
            # must never touch the asyncio.Queue directly: hand each event to
            # the loop with call_soon_threadsafe.
            watch_id = self._find_watch_id(watch_path)
            if not watch_id:
                return
            for event in self._expand_change(change):
                if not self._should_process(event):
                    continue
                loop = self._event_loop
                if loop is None:
                    # Watcher stopped; late events from the observer are moot.
                    logger.debug(f"WatcherAgent stopped, dropping event: {event.path}")
                    continue
                try:
                    loop.call_soon_threadsafe(
                        self._processing_queue.put_nowait, (watch_id, event)
                    )
                except RuntimeError:
                    # The loop closed between the check and the call (shutdown).
                    logger.debug(f"Event loop closed, dropping event: {event.path}")

        return on_change

    @staticmethod
    def _expand_change(change: FileChange) -> list[FileChange]:
        """Split a MOVED event into DELETED (old path) + CREATED (new path).

        Both a rename (``a.pdf`` -> ``b.pdf``) and an editor's atomic save
        (write a temp file, rename it over the original) arrive as MOVED.
        Treated as a delete plus a create, the old path's document is removed
        and the new path is ingested; each half is filtered on its own, so a
        temp file (``.swp``, ``~``) contributes nothing.
        """
        if change.change_type != FileChangeType.MOVED or not change.previous_path:
            return [change]
        return [
            FileChange(
                change_type=FileChangeType.DELETED,
                path=change.previous_path,
                timestamp=change.timestamp,
            ),
            FileChange(
                change_type=FileChangeType.CREATED,
                path=change.path,
                timestamp=change.timestamp,
                file_info=change.file_info,
            ),
        ]

    def _should_process(self, change: FileChange) -> bool:
        """Determine if a file change should trigger ingestion.

        Args:
            change: File change event.

        Returns:
            True if the file should be processed.
        """
        # CREATED/MODIFIED trigger ingestion; DELETED removes the document.
        # Everything else (MOVED, etc.) is ignored.
        if change.change_type not in (
            FileChangeType.CREATED,
            FileChangeType.MODIFIED,
            FileChangeType.DELETED,
        ):
            return False

        # The same filter applies to deletes. Path.suffix works on a path that
        # no longer exists, and a file the watcher would never have ingested
        # (.log, .png, editor swap files) has no document to remove.
        path = Path(change.path)
        if path.suffix.lower() not in self._supported_extensions:
            return False

        # Skip hidden files and directories
        return not any(part.startswith(".") for part in path.parts)

    def _find_watch_id(self, watch_path: str) -> str | None:
        """Find the watch ID for a given watch path.

        Args:
            watch_path: The original watch path.

        Returns:
            Watch ID if found, None otherwise.
        """
        for watch_id, tracker in self._trackers.items():
            if tracker.path == watch_path:
                return watch_id
        return None

    # -------------------------------------------------------------------------
    # Background Processing
    # -------------------------------------------------------------------------

    def _start_processor(self) -> None:
        """Start the background event processor."""
        if self._running:
            return

        self._running = True
        self._event_loop = asyncio.get_running_loop()
        self._processor_task = asyncio.create_task(
            self._process_events(),
            name="watcher_event_processor",
        )
        logger.debug("WatcherAgent: started background event processor")

    async def _process_events(self) -> None:
        """Background task that processes file change events."""
        logger.info("WatcherAgent: event processor started")

        try:
            while self._running:
                try:
                    watch_id, change = await asyncio.wait_for(
                        self._processing_queue.get(),
                        timeout=1.0,
                    )
                except TimeoutError:
                    continue

                await self._handle_file_event(watch_id, change)

        except asyncio.CancelledError:
            logger.info("WatcherAgent: event processor cancelled")
            raise
        except Exception as e:
            logger.error(f"WatcherAgent: event processor error: {e}")
        finally:
            logger.info("WatcherAgent: event processor stopped")

    async def _handle_file_event(
        self,
        watch_id: str,
        change: FileChange,
    ) -> None:
        """Handle a single file change event by running ingestion or deletion.

        Args:
            watch_id: ID of the watch that produced the event.
            change: The file change event.
        """
        tracker = self._trackers.get(watch_id)
        if not tracker:
            logger.warning(f"No tracker for watch {watch_id}")
            return

        file_path = change.path
        logger.info(
            f"WatcherAgent: processing {change.change_type.value} event for {file_path}"
        )
        tracker.last_event_at = datetime.now(tz=UTC)

        # -------------------------------------------------------------------
        # DELETED: remove document + vectors from the system.
        # -------------------------------------------------------------------
        if change.change_type == FileChangeType.DELETED:
            try:
                async with self._db_session_factory() as db:
                    repo = DocumentRepository(db)
                    doc = await repo.get_by_source_path(file_path)
                    if doc is None:
                        logger.debug(
                            f"No tracked document for deleted path {file_path}"
                        )
                        return

                    vector_ids = [
                        chunk.vector_id for chunk in doc.chunks if chunk.vector_id
                    ]
                    if vector_ids:
                        from grimoire.config.settings import get_settings

                        settings = get_settings()
                        vector_store = ChromaDBStore(
                            persist_directory=settings.vector_store.chromadb.path,
                            collection_name=settings.vector_store.chromadb.collection_name,
                            host=settings.vector_store.host,
                            port=settings.vector_store.port,
                        )
                        await vector_store.initialize(
                            collection_name=settings.vector_store.chromadb.collection_name,
                            embedding_dim=1,
                        )
                        await vector_store.delete(vector_ids)
                        logger.info(
                            f"Deleted {len(vector_ids)} vectors for {file_path}"
                        )

                    await db.delete(doc)
                    await db.commit()
                    tracker.files_processed += 1
                    logger.info(f"WatcherAgent: deleted document for {file_path}")

                    # Invalidate query cache so stale results are not served.
                    await self._invalidate_query_cache()
            except Exception as e:
                tracker.files_failed += 1
                logger.error(f"WatcherAgent: error deleting {file_path}: {e}")
            return

        # -------------------------------------------------------------------
        # CREATED / MODIFIED: ingest the file.
        # -------------------------------------------------------------------
        try:
            async with self._db_session_factory() as db:
                result = await self._ingestion_agent.ingest_file(
                    db,
                    file_path,
                    auto_tag=self._auto_tag,
                )

            if result.status == "completed":
                tracker.files_processed += 1
                logger.info(f"WatcherAgent: ingested {file_path}")
                # Invalidate query cache after re-ingestion.
                await self._invalidate_query_cache()
            elif result.status == "skipped":
                logger.debug(f"WatcherAgent: skipped {file_path} (duplicate)")
            else:
                tracker.files_failed += 1
                logger.warning(
                    f"WatcherAgent: failed to ingest {file_path}: "
                    f"{result.error_message}"
                )
        except Exception as e:
            tracker.files_failed += 1
            logger.error(f"WatcherAgent: error processing {file_path}: {e}")

    # -------------------------------------------------------------------------
    # Cache invalidation helper
    # -------------------------------------------------------------------------

    async def _invalidate_query_cache(self) -> None:
        """Invalidate the query cache via the shared QueryAgent singleton.

        Best-effort: failures are logged but not raised.
        """
        try:
            from grimoire.api.dependencies import get_query_agent

            agent = get_query_agent()
            if hasattr(agent, "invalidate_cache"):
                await agent.invalidate_cache()
                logger.debug("WatcherAgent: query cache invalidated")
        except Exception as e:
            logger.warning(f"WatcherAgent: query cache invalidation failed: {e}")

    # -------------------------------------------------------------------------
    # Context Manager
    # -------------------------------------------------------------------------

    async def __aenter__(self) -> WatcherAgent:
        """Async context manager entry."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Async context manager exit - stops all watches."""
        await self.stop_all()
