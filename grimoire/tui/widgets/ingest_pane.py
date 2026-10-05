"""The Ingest pane: send files from this machine to the API.

A path goes into the box, ``Enter`` queues it, and the files upload one at a
time through ``GrimoireClient.upload``.  That reads the file *here* and sends
the bytes, so it works when the TUI runs over SSH or against a remote server,
unlike the server-path endpoint (which only accepts paths on the server).

Single files only.  A directory is refused with a message rather than expanded.

The client is synchronous and a thread cannot be killed, so the queue is
serialised by hand: one worker runs at a time, and its outcome (posted as a
message) is what starts the next.  One failure never stops the rest.

File names and server messages are untrusted text, so every table cell is a
Rich ``Text`` and the status lines are created with ``markup=False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Checkbox, DataTable, Input, Static
from textual.worker import get_current_worker

from grimoire.api.schemas import IngestResultResponse
from grimoire.client.client import GrimoireClient
from grimoire.tui.errors import reachability, user_message
from grimoire.tui.formatting import truncate
from grimoire.tui.messages import ConnectionReport

_HELP = (
    "Type the path of a file on this machine and press Enter. "
    "Files upload one at a time."
)
_EMPTY = "Nothing ingested yet."
_STATUS_WIDTH = 10
_NAME_MAX = 36
_MIN_DETAIL = 12
_MAX_QUEUED = 50
_STATUS_STYLES = {"done": "green", "skipped": "yellow", "failed": "red"}


def normalize_path(text: str) -> Path | None:
    """The path a user typed or pasted, or ``None`` if there is none.

    File managers and shells paste paths wrapped in quotes, and people type
    ``~``; both are undone.  Nothing else is guessed at.
    """
    cleaned = text.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        cleaned = cleaned[1:-1].strip()
    if not cleaned:
        return None
    return Path(cleaned).expanduser()


@dataclass
class _Job:
    """One file on its way to the server."""

    id: int
    path: Path
    auto_tag: bool
    status: str = "queued"
    detail: str = ""


class IngestPane(Vertical):
    """Queue files for upload and show how each one went.

    Args:
        client: API client.  Owned by the caller.
        id: Widget id.

    Attributes:
        status_text: Plain text of the line under the box (errors and hints).
        footer_text: Plain text of the summary line under the table.
    """

    class Done(Message):
        """An upload finished (the server answered, even if the answer was 'failed')."""

        def __init__(self, job_id: int, result: IngestResultResponse) -> None:
            super().__init__()
            self.job_id = job_id
            self.result = result

    class Failed(Message):
        """An upload raised before the server could answer."""

        def __init__(self, job_id: int, error: BaseException) -> None:
            super().__init__()
            self.job_id = job_id
            self.error = error

    class Completed(Message):
        """A file was added to the corpus, so other panes' data is out of date."""

    def __init__(self, client: GrimoireClient, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._client = client
        self._jobs: list[_Job] = []
        self._next_id = 0
        self._uploading = False
        self.status_text = _HELP
        self.footer_text = ""

    def set_client(self, client: GrimoireClient) -> None:
        """Use a different client from now on (the API key was replaced).

        An upload already running keeps the client it started with; queued
        files use the new one when their turn comes.
        """
        self._client = client

    # -- layout ---------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="ingest-input-row"):
            yield Input(placeholder="Path to a file, then Enter", id="ingest-path")
            yield Checkbox("Auto-tag", value=True, id="ingest-autotag")
        yield Static(_HELP, id="ingest-status", markup=False)
        yield DataTable(id="ingest-table", cursor_type="row", zebra_stripes=True)
        yield Static(_EMPTY, id="ingest-empty", markup=False)
        yield Static("", id="ingest-footer", markup=False)

    def on_mount(self) -> None:
        self.query_one("#ingest-table").display = False

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self._refresh_view)

    # -- hooks called by the app ---------------------------------------------

    def focus_primary(self) -> None:
        self.query_one("#ingest-path", Input).focus()

    # -- submitting -----------------------------------------------------------

    @on(Input.Submitted, "#ingest-path")
    def _on_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        path = normalize_path(event.value)
        if path is None:
            self._set_status("Enter the path of a file.", error=True)
            return
        if path.is_dir():
            self._set_status(
                "That is a directory; the TUI ingests single files. "
                "Enter a file path.",
                error=True,
            )
            return
        if not path.is_file():
            self._set_status(f"No such file: {path}", error=True)
            return
        if sum(job.status == "queued" for job in self._jobs) >= _MAX_QUEUED:
            self._set_status(
                "Too many files waiting; let some finish first.", error=True
            )
            return
        auto_tag = self.query_one("#ingest-autotag", Checkbox).value
        self._jobs.append(_Job(self._next_id, path, auto_tag))
        self._next_id += 1
        event.input.value = ""
        self._set_status(_HELP)
        self._refresh_view()
        self._pump()

    # -- the queue ------------------------------------------------------------

    def _pump(self) -> None:
        """Start the next waiting file, if nothing is uploading."""
        if self._uploading:
            return
        job = next((j for j in self._jobs if j.status == "queued"), None)
        if job is None:
            return
        job.status = "uploading"
        self._uploading = True
        self._refresh_view()
        self._upload(job.id, job.path, job.auto_tag, self._client)

    @work(thread=True, group="ingest", exit_on_error=False)
    def _upload(
        self, job_id: int, path: Path, auto_tag: bool, client: GrimoireClient
    ) -> None:
        """Upload one file off the event loop and post the outcome."""
        worker = get_current_worker()
        message: Message
        try:
            message = self.Done(job_id, client.upload(path, auto_tag=auto_tag))
        except Exception as exc:
            message = self.Failed(job_id, exc)
        if not worker.is_cancelled:
            self.post_message(message)

    def _job(self, job_id: int) -> _Job | None:
        return next((j for j in self._jobs if j.id == job_id), None)

    def on_ingest_pane_done(self, message: Done) -> None:
        job, result = self._job(message.job_id), message.result
        if job is None:
            return
        if result.status == "failed":
            job.status, job.detail = (
                "failed",
                result.error_message or "Ingestion failed",
            )
        elif result.status == "skipped":
            job.status, job.detail = "skipped", "Already in the corpus"
        else:
            job.status = "done"
            job.detail = f"{result.chunks_created} chunks · {result.duration_ms} ms"
            self.post_message(self.Completed())
        self.post_message(ConnectionReport(reachable=True))
        self._finished()

    def on_ingest_pane_failed(self, message: Failed) -> None:
        job = self._job(message.job_id)
        if job is None:
            return
        job.status, job.detail = "failed", user_message(message.error)
        reachable = reachability(message.error)
        if reachable is not None:
            self.post_message(ConnectionReport(reachable=reachable))
        self._finished()

    def _finished(self) -> None:
        self._uploading = False
        self._refresh_view()
        self._pump()

    # -- rendering ------------------------------------------------------------

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = text
        line = self.query_one("#ingest-status", Static)
        line.update(text)
        line.set_class(error, "-error")

    def _refresh_view(self) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#ingest-table", DataTable)
        has_rows = bool(self._jobs)
        if has_rows and not table.display:
            self.call_after_refresh(self._refresh_view)  # re-measure once shown
        table.display = has_rows
        self.query_one("#ingest-empty").display = not has_rows
        self._fill_table(table)
        counts = {s: sum(j.status == s for j in self._jobs) for s in _ORDER}
        self.footer_text = (
            " · ".join(f"{counts[s]} {s}" for s in _ORDER if counts[s]) or ""
        )
        self.query_one("#ingest-footer", Static).update(self.footer_text)

    def _fill_table(self, table: DataTable[Any]) -> None:
        # A table that was hidden until a moment ago has no width yet; fall back
        # on the pane's (less its own padding) and re-measure once laid out.
        width = table.size.width or max(0, self.size.width - 2)
        available = max(0, width - table.styles.scrollbar_size_vertical)
        name_w = min(_NAME_MAX, max([4] + [len(j.path.name) for j in self._jobs]))
        # Each of three columns costs two characters of cell padding.
        detail_w = max(0, available - name_w - _STATUS_WIDTH - 6)
        if detail_w < _MIN_DETAIL:
            detail_w = 0
        previous = table.cursor_row
        table.clear(columns=True)
        # Explicit widths: the table must not measure its own cells (see the
        # Documents pane for the clipped-frame bug that causes).
        table.add_column("File", width=name_w)
        table.add_column("Status", width=_STATUS_WIDTH)
        if detail_w:
            table.add_column("Detail", width=detail_w)
        for job in self._jobs:
            cells = [
                Text(truncate(job.path.name, name_w)),
                Text(job.status, style=_STATUS_STYLES.get(job.status, "dim")),
            ]
            if detail_w:
                cells.append(Text(truncate(" ".join(job.detail.split()), detail_w)))
            table.add_row(*cells)
        if self._jobs:
            table.move_cursor(row=min(previous, len(self._jobs) - 1))


_ORDER = ("uploading", "queued", "done", "skipped", "failed")
