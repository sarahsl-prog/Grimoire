"""Watch management API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from grimoire.api.auth import get_api_key, require_min_tier
from grimoire.api.schemas import WatcherStatsResponse, WatchResponse, WatchStartRequest
from grimoire.db.models import ApiKey, ApiKeyTier
from grimoire.utils.path_guard import (
    PathNotAllowedError,
    configured_roots,
    resolve_allowed,
)

router = APIRouter(prefix="/watch", tags=["watch"])

# Backends whose path is a directory on the server's own filesystem and so must
# stay inside api.allowed_roots.  Cloud/rclone paths are remote names, not
# local paths, so the filesystem guard does not apply to them.
_LOCAL_BACKENDS = frozenset({"local", "usb"})
_KNOWN_BACKENDS = _LOCAL_BACKENDS | {"rclone", "gdrive", "onedrive"}

# In-process watcher instance (set during app lifespan if watching is enabled)
_watcher: Any = None


def set_watcher(watcher: Any) -> None:
    """Set the active watcher agent (called from app lifespan)."""
    global _watcher
    _watcher = watcher


def _get_watcher() -> Any:
    if _watcher is None:
        raise HTTPException(
            status_code=503,
            detail="Watcher not initialized. Start the server with --watch.",
        )
    return _watcher


@router.post("/start", response_model=WatchResponse, status_code=201)
async def start_watch(
    request: Request,
    body: WatchStartRequest,
    api_key: ApiKey = Depends(require_min_tier(ApiKeyTier.DEV)),
) -> WatchResponse:
    """Start watching a path for changes."""
    watcher = _get_watcher()
    if body.backend not in _KNOWN_BACKENDS:
        raise HTTPException(status_code=400, detail="Unknown watch backend.")

    path = body.path
    if body.backend in _LOCAL_BACKENDS:
        try:
            # Watch the resolved path so a symlink swapped in later cannot
            # redirect the watch outside the allowed roots.
            path = str(resolve_allowed(body.path, configured_roots()))
        except PathNotAllowedError as exc:
            raise HTTPException(
                status_code=403 if exc.forbidden else 400, detail=exc.message
            ) from None

    try:
        watch_id = await watcher.watch(
            path,
            backend=body.backend,
            recursive=body.recursive,
            poll_interval=body.poll_interval,
        )
    except ValueError as exc:
        # Already watched / invalid path: the caller's problem, not a 500.
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return WatchResponse(
        watch_id=watch_id,
        path=path,
        backend=body.backend,
        is_running=True,
    )


@router.delete("/{watch_id}", status_code=204)
async def stop_watch(
    watch_id: str,
    request: Request,
    api_key: ApiKey = Depends(require_min_tier(ApiKeyTier.DEV)),
) -> None:
    """Stop a specific watch."""
    watcher = _get_watcher()
    success = await watcher.unwatch(watch_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Watch {watch_id} not found")


@router.get("/status", response_model=WatcherStatsResponse)
async def get_watch_status(
    request: Request,
    api_key: ApiKey = Depends(get_api_key),
) -> WatcherStatsResponse:
    """Get watcher statistics."""
    watcher = _get_watcher()
    stats = watcher.get_status()
    return WatcherStatsResponse(
        active_watches=stats.active_watches,
        total_files_processed=stats.total_files_processed,
        total_files_failed=stats.total_files_failed,
        watches=[
            WatchResponse(
                watch_id=w.watch_id,
                path=w.path,
                backend=w.backend,
                is_running=w.is_running,
            )
            for w in stats.watches
        ],
    )
