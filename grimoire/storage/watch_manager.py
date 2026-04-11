"""Hybrid watch manager for local and cloud storage paths.

This module provides a unified interface for watching file changes across
local filesystems (using watchdog) and cloud storage (using polling).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from loguru import logger
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver as Observer

from grimoire.storage.base import (
    FileChange,
    FileChangeType,
    StorageBackend,
)


class WatchType(Enum):
    """Type of watch mechanism."""

    LOCAL = "local"
    CLOUD = "cloud"


@dataclass
class WatchConfig:
    """Configuration for a watched path."""

    path: str
    backend: StorageBackend
    callback: Callable[[FileChange], Any]
    recursive: bool = True
    poll_interval: int = 300
    watch_type: WatchType = WatchType.LOCAL


@dataclass
class ActiveWatch:
    """Represents an active watch session."""

    config: WatchConfig
    local_observer: BaseObserver | None = None
    cloud_task: asyncio.Task[Any] | None = None
    last_poll_time: datetime = field(default_factory=datetime.now)
    is_running: bool = False


class WatchManager:
    """Manages multiple watchers for local and cloud storage paths."""

    def __init__(
        self,
        default_poll_intervals: dict[StorageBackend, int] | None = None,
        max_local_watches: int = 100,
    ) -> None:
        """Initialize the watch manager."""
        self._watches: dict[str, ActiveWatch] = {}
        self._max_local_watches = max_local_watches
        self._local_watch_count = 0
        self._poller = CloudStoragePoller()

        self._default_poll_intervals: dict[StorageBackend, int] = {
            StorageBackend.LOCAL: 0,
            StorageBackend.USB: 0,
            StorageBackend.RCLONE: 60,
            StorageBackend.GOOGLE_DRIVE: 300,
            StorageBackend.ONE_DRIVE: 300,
        }

        if default_poll_intervals:
            self._default_poll_intervals.update(default_poll_intervals)

        logger.debug(
            f"WatchManager initialized (max_local_watches={max_local_watches})"
        )

    def _is_cloud_path(self, backend: StorageBackend) -> bool:
        """Check if a backend type requires cloud polling."""
        return backend in {
            StorageBackend.GOOGLE_DRIVE,
            StorageBackend.ONE_DRIVE,
            StorageBackend.RCLONE,
        }

    def _get_poll_interval(self, backend: StorageBackend, override: int | None) -> int:
        """Get the poll interval for a backend."""
        if override is not None:
            return override
        return self._default_poll_intervals.get(backend, 300)

    async def start_watch(
        self,
        path: str,
        backend: str | StorageBackend,
        callback: Callable[[FileChange], Any],
        *,
        recursive: bool = True,
        poll_interval: int | None = None,
    ) -> str:
        """Start watching a path for changes."""
        if isinstance(backend, str):
            try:
                backend = StorageBackend(backend)
            except ValueError as e:
                raise ValueError(f"Invalid backend: {backend}") from e

        watch_id = f"{backend.value}:{path}"

        if watch_id in self._watches:
            raise ValueError(
                f"Already watching path: {path} with backend: {backend.value}"
            )

        is_cloud = self._is_cloud_path(backend)
        watch_type = WatchType.CLOUD if is_cloud else WatchType.LOCAL
        effective_poll_interval = self._get_poll_interval(backend, poll_interval)

        config = WatchConfig(
            path=path,
            backend=backend,
            callback=callback,
            recursive=recursive,
            poll_interval=effective_poll_interval,
            watch_type=watch_type,
        )

        active_watch = ActiveWatch(config=config)

        if is_cloud:
            await self._start_cloud_watch(active_watch)
        else:
            self._start_local_watch(active_watch)

        self._watches[watch_id] = active_watch
        logger.info(f"Started {watch_type.value} watch on {path}")
        return watch_id

    def _start_local_watch(self, active_watch: ActiveWatch) -> None:
        """Start a local watchdog observer."""
        if self._local_watch_count >= self._max_local_watches:
            raise RuntimeError(
                f"Maximum number of local watches ({self._max_local_watches}) exceeded"
            )

        path = active_watch.config.path
        if not Path(path).exists():
            raise RuntimeError(f"Path does not exist: {path}")

        event_handler = _WatchdogEventHandler(
            callback=active_watch.config.callback,
            watch_path=path,
        )

        observer = Observer()
        observer.schedule(
            event_handler,
            path=path,
            recursive=active_watch.config.recursive,
        )
        observer.start()

        active_watch.local_observer = observer
        active_watch.is_running = True
        self._local_watch_count += 1
        logger.debug(f"Started watchdog observer for {path}")

    async def _start_cloud_watch(self, active_watch: ActiveWatch) -> None:
        """Start a cloud polling task."""
        task = asyncio.create_task(
            self._cloud_poll_loop(active_watch),
            name=f"cloud_watch_{active_watch.config.path}",
        )
        active_watch.cloud_task = task
        active_watch.is_running = True
        active_watch.last_poll_time = datetime.now(tz=timezone.utc)
        logger.debug(f"Started cloud polling for {active_watch.config.path}")

    async def _cloud_poll_loop(self, active_watch: ActiveWatch) -> None:
        """Main polling loop for cloud storage paths."""
        config = active_watch.config
        logger.info(f"Cloud polling started for {config.path}")

        try:
            while active_watch.is_running:
                try:
                    await asyncio.sleep(config.poll_interval)
                    changes = await self._poller.poll_changes(
                        config.backend,
                        config.path,
                        active_watch.last_poll_time,
                    )
                    for change in changes:
                        try:
                            if asyncio.iscoroutinefunction(config.callback):
                                asyncio.create_task(
                                    self._invoke_async_callback(config.callback, change)
                                )
                            else:
                                config.callback(change)
                        except Exception as e:
                            logger.error(f"Error invoking watch callback: {e}")
                    active_watch.last_poll_time = datetime.now(tz=timezone.utc)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error during cloud poll for {config.path}: {e}")
                    await asyncio.sleep(config.poll_interval)
        except asyncio.CancelledError:
            logger.info(f"Cloud polling cancelled for {config.path}")
            raise
        finally:
            logger.info(f"Cloud polling stopped for {config.path}")

    async def _invoke_async_callback(
        self, callback: Callable[[FileChange], Any], change: FileChange
    ) -> None:
        """Invoke an async callback, suppressing errors."""
        try:
            await callback(change)
        except Exception as e:
            logger.error(f"Error in async watch callback: {e}")

    async def stop_watch(self, watch_id: str) -> bool:
        """Stop watching a specific path."""
        if watch_id not in self._watches:
            logger.warning(f"Watch not found: {watch_id}")
            return False

        active_watch = self._watches[watch_id]
        config = active_watch.config

        if not active_watch.is_running:
            del self._watches[watch_id]
            return True

        if config.watch_type == WatchType.LOCAL:
            self._stop_local_watch(active_watch)
        else:
            await self._stop_cloud_watch(active_watch)

        del self._watches[watch_id]
        logger.info(f"Stopped {config.watch_type.value} watch on {config.path}")
        return True

    def _stop_local_watch(self, active_watch: ActiveWatch) -> None:
        """Stop a local watchdog observer."""
        if active_watch.local_observer:
            active_watch.local_observer.stop()
            active_watch.local_observer.join(timeout=5)
            if active_watch.local_observer.is_alive():
                logger.warning("Watchdog observer did not stop cleanly")
            active_watch.local_observer = None
        active_watch.is_running = False
        self._local_watch_count -= 1

    async def _stop_cloud_watch(self, active_watch: ActiveWatch) -> None:
        """Stop a cloud polling task."""
        active_watch.is_running = False
        if active_watch.cloud_task:
            active_watch.cloud_task.cancel()
            try:
                await active_watch.cloud_task
            except asyncio.CancelledError:
                pass
            active_watch.cloud_task = None

    async def stop_all(self) -> None:
        """Stop all active watches."""
        watch_ids = list(self._watches.keys())
        for watch_id in watch_ids:
            await self.stop_watch(watch_id)
        logger.info("All watches stopped")

    def list_watches(self) -> list[dict[str, Any]]:
        """List all active watches."""
        return [
            {
                "watch_id": watch_id,
                "path": watch.config.path,
                "backend": watch.config.backend.value,
                "watch_type": watch.config.watch_type.value,
                "is_running": watch.is_running,
                "recursive": watch.config.recursive,
                "poll_interval": watch.config.poll_interval,
                "last_poll_time": (
                    watch.last_poll_time.isoformat() if watch.last_poll_time else None
                ),
            }
            for watch_id, watch in self._watches.items()
        ]

    def get_watch(self, watch_id: str) -> dict[str, Any] | None:
        """Get information about a specific watch."""
        if watch_id not in self._watches:
            return None
        watch = self._watches[watch_id]
        return {
            "watch_id": watch_id,
            "path": watch.config.path,
            "backend": watch.config.backend.value,
            "watch_type": watch.config.watch_type.value,
            "is_running": watch.is_running,
            "recursive": watch.config.recursive,
            "poll_interval": watch.config.poll_interval,
            "last_poll_time": (
                watch.last_poll_time.isoformat() if watch.last_poll_time else None
            ),
        }

    async def __aenter__(self) -> WatchManager:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.stop_all()


class _WatchdogEventHandler(FileSystemEventHandler):
    """Internal watchdog event handler that converts to FileChange events."""

    def __init__(
        self,
        callback: Callable[[FileChange], Any],
        watch_path: str,
    ) -> None:
        super().__init__()
        self._callback = callback
        self._watch_path = watch_path

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Handle any watchdog event."""
        if isinstance(event, (FileCreatedEvent, DirCreatedEvent)):
            change_type = FileChangeType.CREATED
        elif isinstance(event, (FileDeletedEvent, DirDeletedEvent)):
            change_type = FileChangeType.DELETED
        elif isinstance(event, (FileMovedEvent, DirMovedEvent)):
            change_type = FileChangeType.MOVED
        elif isinstance(event, (FileModifiedEvent, DirModifiedEvent)):
            change_type = FileChangeType.MODIFIED
        else:
            logger.debug(f"Unknown event type: {type(event)}")
            return

        src_path_raw = event.src_path
        src_path: str
        if isinstance(src_path_raw, bytes):
            src_path = src_path_raw.decode("utf-8")
        else:
            src_path = src_path_raw

        dest_path: str | None = None
        if hasattr(event, "dest_path"):
            dest_path_raw = event.dest_path
            if isinstance(dest_path_raw, bytes):
                dest_path = dest_path_raw.decode("utf-8")
            elif isinstance(dest_path_raw, str):
                dest_path = dest_path_raw

        change = FileChange(
            change_type=change_type,
            path=src_path,
            previous_path=dest_path,
            timestamp=datetime.now(),
            file_info=None,
        )

        try:
            if asyncio.iscoroutinefunction(self._callback):
                asyncio.create_task(self._async_callback_wrapper(change))
            else:
                self._callback(change)
        except Exception as e:
            logger.error(f"Error invoking watch callback: {e}")

    async def _async_callback_wrapper(self, change: FileChange) -> None:
        try:
            await self._callback(change)
        except Exception as e:
            logger.error(f"Error in async watch callback: {e}")

    def dispatch(self, event: FileSystemEvent) -> None:
        self.on_any_event(event)


class CloudStoragePoller:
    """Helper class for polling cloud storage changes."""

    def __init__(self) -> None:
        self._page_tokens: dict[str, str] = {}
        self._adapters: dict[StorageBackend, Any] = {}

    async def poll_changes(
        self,
        backend: StorageBackend,
        path: str,
        since: datetime,
    ) -> list[FileChange]:
        logger.debug(f"Polling {backend.value}:{path} for changes since {since}")
        adapter = self._get_adapter(backend)
        if adapter is None:
            return []
        try:
            changes = await adapter.list_changes(since, path)
            return changes
        except NotImplementedError:
            logger.warning(f"Adapter {backend.value} does not support list_changes()")
            return []
        except Exception as e:
            logger.error(f"Error polling {backend.value}: {e}")
            return []

    def _get_adapter(self, backend: StorageBackend) -> Any:
        """Get or create a cloud storage adapter for the given backend."""
        if backend in self._adapters:
            return self._adapters[backend]

        try:
            if backend == StorageBackend.GOOGLE_DRIVE:
                from grimoire.config.settings import get_settings
                settings = get_settings()
                if settings.cloud and settings.cloud.google:
                    from grimoire.storage.gdrive import GoogleDriveAdapter
                    self._adapters[backend] = GoogleDriveAdapter(settings.cloud.google)
                    return self._adapters[backend]
            elif backend == StorageBackend.ONE_DRIVE:
                from grimoire.config.settings import get_settings
                settings = get_settings()
                if settings.cloud and settings.cloud.microsoft:
                    from grimoire.storage.onedrive import OneDriveAdapter
                    self._adapters[backend] = OneDriveAdapter(settings.cloud.microsoft)
                    return self._adapters[backend]
        except Exception as e:
            logger.error(f"Failed to create {backend.value} adapter: {e}")
        return None

    def get_page_token(self, path: str) -> str | None:
        return self._page_tokens.get(path)

    def set_page_token(self, path: str, token: str) -> None:
        self._page_tokens[path] = token
