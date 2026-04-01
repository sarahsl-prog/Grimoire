"""Watch management API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from grimoire.api.schemas import WatchResponse, WatchStartRequest, WatcherStatsResponse

router = APIRouter(prefix="/watch", tags=["watch"])

# In-process watcher instance (set during app lifespan if watching is enabled)
_watcher = None


def set_watcher(watcher: object) -> None:
    """Set the active watcher agent (called from app lifespan)."""
    global _watcher
    _watcher = watcher


def _get_watcher():
    if _watcher is None:
        raise HTTPException(status_code=503, detail="Watcher not initialized. Start the server with --watch.")
    return _watcher


@router.post("/start", response_model=WatchResponse, status_code=201)
async def start_watch(request: WatchStartRequest) -> WatchResponse:
    """Start watching a path for changes."""
    watcher = _get_watcher()
    watch_id = await watcher.watch(
        request.path,
        backend=request.backend,
        recursive=request.recursive,
        poll_interval=request.poll_interval,
    )
    return WatchResponse(
        watch_id=watch_id,
        path=request.path,
        backend=request.backend,
        is_running=True,
    )


@router.delete("/{watch_id}", status_code=204)
async def stop_watch(watch_id: str) -> None:
    """Stop a specific watch."""
    watcher = _get_watcher()
    success = await watcher.unwatch(watch_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Watch {watch_id} not found")


@router.get("/status", response_model=WatcherStatsResponse)
async def get_watch_status() -> WatcherStatsResponse:
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
