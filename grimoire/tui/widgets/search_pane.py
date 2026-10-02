"""The Search / Ask pane.

Two modes over one query box.  *Ask* runs the full RAG pipeline (retrieval plus
a generated answer); *Search* is retrieval only, which is the quick way to check
whether the right chunks come back before spending an LLM generation on them.

Things this pane is careful about, because a thread-based UI gets them wrong:

* **Stale results.**  The client is synchronous, so a request runs in a thread
  worker that cannot be killed.  Every request carries an id; only the result
  whose id is still current is shown.  Abandoning (Esc) and resubmitting both
  advance the id, so a late answer from the old request is dropped.
* **Untrusted text.**  The answer comes from an LLM and titles and chunk text
  from ingested files, which in the security corpus may be hostile.  Nothing
  from the server is ever parsed as Rich markup: the answer goes through the
  Markdown widget with link-opening disabled, list rows are ``Text`` objects,
  and every other widget that shows server text is created with
  ``markup=False``.
* **Errors.**  Only ``ClientError`` messages (written for people) or a generic line
  reach the screen; the previous results stay put so a failed retry loses
  nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.timer import Timer
from textual.validation import ValidationResult, Validator
from textual.widgets import (
    Button,
    Collapsible,
    Input,
    Label,
    LoadingIndicator,
    Markdown,
    OptionList,
    RadioButton,
    RadioSet,
    Select,
    Static,
)
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from grimoire.api.schemas import QueryResponse, SearchResponse
from grimoire.client.client import GrimoireClient
from grimoire.tui.errors import reachability, user_message
from grimoire.tui.formatting import (
    SEVERITIES,
    SOURCE_TYPES,
    TOP_K_MAX,
    TOP_K_MIN,
    SourceView,
    build_filter_dict,
    parse_top_k,
    sources_from_query,
    sources_from_search,
    truncate,
)
from grimoire.tui.messages import ConnectionReport
from grimoire.tui.screens.document_detail import DocumentDetailScreen

Mode = Literal["ask", "search"]

_TOP_K_HELP = f"Top-k must be a whole number from {TOP_K_MIN} to {TOP_K_MAX}."
_NO_ANSWER = "No answer was generated."
_NO_SOURCES = "No sources were returned for this query."
_ABANDONED = "Abandoned. The server may still be working on it."


class _TopKValidator(Validator):
    """Input validation backed by ``parse_top_k``, the single source of truth."""

    def validate(self, value: str) -> ValidationResult:
        if parse_top_k(value) is not None:
            return self.success()
        return self.failure(_TOP_K_HELP)


@dataclass(frozen=True)
class _Request:
    """Everything a worker needs, captured on the event loop at submit time."""

    id: int
    mode: Mode
    query: str
    top_k: int
    filters: dict[str, Any] | None


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


class SearchPane(Vertical):
    """Query the corpus and inspect what retrieval returned.

    Args:
        client: API client.  Owned by the caller.
        id: Widget id.

    Attributes:
        in_flight: Whether a request is currently running.
        sources: The sources currently listed, in server order.
        status_text: Plain text of the status line (progress or error).
        meta_text: Plain text of the line under the query (model, counts).
        answer_text: The answer currently shown (Ask mode).
        preview_text: Plain text of the source preview.
    """

    BINDINGS = [
        Binding("escape", "abandon", "Abandon request"),
        Binding("o", "open_document", "Open document"),
    ]

    class Completed(Message):
        """A request finished successfully."""

        def __init__(
            self, request_id: int, mode: Mode, result: QueryResponse | SearchResponse
        ) -> None:
            super().__init__()
            self.request_id = request_id
            self.mode = mode
            self.result = result

    class Failed(Message):
        """A request raised."""

        def __init__(self, request_id: int, error: BaseException) -> None:
            super().__init__()
            self.request_id = request_id
            self.error = error

    def __init__(self, client: GrimoireClient, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._client = client
        self._request_id = 0
        self._ticker: Timer | None = None
        self._started = 0.0
        self.in_flight = False
        self.sources: list[SourceView] = []
        self.status_text = ""
        self.meta_text = ""
        self.answer_text = ""
        self.preview_text = ""

    def set_client(self, client: GrimoireClient) -> None:
        """Use a different client from now on (the API key was replaced).

        A request already running keeps the client it started with.
        """
        self._client = client

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="query-row"):
            yield Input(
                placeholder="Ask a question, or search for a phrase",
                id="query-input",
            )
            yield Button("Run", id="run-button", variant="primary")
        with Horizontal(id="options-row"):
            with RadioSet(id="mode-set"):
                yield RadioButton("Ask", value=True, id="mode-ask")
                yield RadioButton("Search", id="mode-search")
            yield Label("Top-k", id="top-k-label")
            yield Input("5", id="top-k", validators=[_TopKValidator()])
            yield LoadingIndicator(id="spinner")
        with Collapsible(title="Filters", collapsed=True, id="filters"):
            yield Input(placeholder="tags (comma-separated)", id="tags-input")
            yield Select(
                [(value, value) for value in SOURCE_TYPES],
                prompt="Any source type",
                id="source-type",
            )
            yield Select(
                [(value, value) for value in SEVERITIES],
                prompt="Any severity",
                id="severity",
            )
            yield Input(placeholder="CVE id, e.g. CVE-2024-1234", id="cve-input")
        yield Static("", id="status-line", markup=False)
        yield Static("", id="meta-line", markup=False)
        with VerticalScroll(id="answer-box"):
            yield Markdown("", id="answer", open_links=False)
        with Horizontal(id="sources"):
            yield OptionList(id="source-list")
            with VerticalScroll(id="preview-scroll"):
                yield Static("", id="source-preview", markup=False)

    def on_mount(self) -> None:
        self.query_one("#spinner").display = False
        self.focus_primary()

    def focus_primary(self) -> None:
        """Put the cursor in the query box (called when the tab is shown)."""
        self.query_one("#query-input", Input).focus()

    # -- submitting ---------------------------------------------------------

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    @on(Button.Pressed, "#run-button")
    def _on_run_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._submit()

    @property
    def mode(self) -> Mode:
        return "search" if self.query_one("#mode-search", RadioButton).value else "ask"

    def _submit(self) -> None:
        if self.in_flight:
            return  # one request at a time; the Run button is disabled anyway

        query = self.query_one("#query-input", Input).value.strip()
        if not query:
            self._reject("Enter a question first.")
            return

        top_k_box = self.query_one("#top-k", Input)
        top_k = parse_top_k(top_k_box.value)
        if top_k is None:
            top_k_box.validate(top_k_box.value)  # make the box show as invalid
            self._reject(_TOP_K_HELP)
            return

        source_type = self.query_one("#source-type", Select)
        severity = self.query_one("#severity", Select)
        filters = build_filter_dict(
            tags=self.query_one("#tags-input", Input).value,
            source_type=None if source_type.is_blank() else str(source_type.value),
            severity=None if severity.is_blank() else str(severity.value),
            cve_id=self.query_one("#cve-input", Input).value,
        )

        self._request_id += 1
        request = _Request(self._request_id, self.mode, query, top_k, filters)
        self._begin()
        self._run_query(request)

    def _reject(self, message: str) -> None:
        """Refuse to send: say why inline and as a toast.  No request is made."""
        self._set_status(message, error=True)
        self.app.notify(message, severity="warning", markup=False)

    # -- request lifecycle --------------------------------------------------

    def _begin(self) -> None:
        self.in_flight = True
        self._started = time.monotonic()
        self.query_one("#run-button", Button).disabled = True
        self.query_one("#spinner").display = True
        self._set_status(self._progress_text())
        # A visible clock: an Ask can legitimately take minutes, and a frozen
        # screen looks the same as a hung one.
        self._ticker = self.set_interval(1.0, self._tick)
        self.refresh_bindings()

    def _finish(self) -> None:
        self.in_flight = False
        if self._ticker is not None:
            self._ticker.stop()
            self._ticker = None
        self.query_one("#run-button", Button).disabled = False
        self.query_one("#spinner").display = False
        self.refresh_bindings()

    def _progress_text(self) -> str:
        elapsed = int(time.monotonic() - self._started)
        return f"Working… {elapsed} s (Esc to abandon)"

    def _tick(self) -> None:
        if self.in_flight:
            self._set_status(self._progress_text())

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Hide the Esc binding when there is nothing to abandon, so Esc stays
        # free for whatever else wants it.
        if action == "abandon":
            return self.in_flight
        if action == "open_document":
            # Only when the highlighted source names a document to open.
            return self._highlighted_document_id() is not None
        return super().check_action(action, parameters)

    def _highlighted_document_id(self) -> str | None:
        """The document behind the highlighted source, if it names one."""
        index = self.query_one("#source-list", OptionList).highlighted
        if index is None or not 0 <= index < len(self.sources):
            return None
        return self.sources[index].document_id or None

    def action_open_document(self) -> None:
        document_id = self._highlighted_document_id()
        if document_id:
            self.app.push_screen(DocumentDetailScreen(self._client, document_id))

    @on(OptionList.OptionSelected, "#source-list")
    def _on_source_selected(self, event: OptionList.OptionSelected) -> None:
        """``Enter`` on a source opens its document, like ``o``."""
        event.stop()
        self.action_open_document()

    def action_abandon(self) -> None:
        if not self.in_flight:
            return
        # Cancels the worker's bookkeeping; the HTTP call itself keeps running
        # in its thread.  Advancing the id makes sure its late result, if it
        # ever arrives, is recognised as stale and dropped.
        self.workers.cancel_group(self, "query")
        self._request_id += 1
        self._finish()
        self._set_status(_ABANDONED)

    @work(thread=True, exclusive=True, group="query", exit_on_error=False)
    def _run_query(self, request: _Request) -> None:
        """Call the API off the event loop and post the outcome."""
        worker = get_current_worker()
        message: Message
        try:
            if request.mode == "ask":
                result: QueryResponse | SearchResponse = self._client.ask(
                    request.query, top_k=request.top_k, filter_dict=request.filters
                )
            else:
                result = self._client.search(
                    request.query, top_k=request.top_k, filter_dict=request.filters
                )
            message = self.Completed(request.id, request.mode, result)
        except Exception as exc:
            message = self.Failed(request.id, exc)
        if not worker.is_cancelled:
            self.post_message(message)

    # -- outcomes -----------------------------------------------------------

    def _is_stale(self, request_id: int) -> bool:
        return request_id != self._request_id or not self.in_flight

    async def on_search_pane_completed(self, message: Completed) -> None:
        if self._is_stale(message.request_id):
            return
        self._finish()
        self._set_status("")
        self.post_message(ConnectionReport(reachable=True))

        result = message.result
        if message.mode == "ask" and isinstance(result, QueryResponse):
            await self._show_answer(result)
            self._show_sources(sources_from_query(result))
        elif isinstance(result, SearchResponse):
            await self._hide_answer()
            self._set_meta(
                f"{_plural(result.total_results, 'result')} · "
                f"{result.duration_ms / 1000:.1f} s"
            )
            self._show_sources(sources_from_search(result))

    def on_search_pane_failed(self, message: Failed) -> None:
        if self._is_stale(message.request_id):
            return
        self._finish()
        text = user_message(message.error)
        self._set_status(text, error=True)
        # markup=False: the text may come from the server.
        self.app.notify(text, severity="error", markup=False)
        reachable = reachability(message.error)
        if reachable is not None:
            self.post_message(ConnectionReport(reachable=reachable))

    # -- rendering ----------------------------------------------------------

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = text
        line = self.query_one("#status-line", Static)
        line.update(text)
        line.set_class(error, "-error")

    def _set_meta(self, text: str) -> None:
        self.meta_text = text
        self.query_one("#meta-line", Static).update(text)

    async def _show_answer(self, result: QueryResponse) -> None:
        self.answer_text = result.answer.strip() or _NO_ANSWER
        self.query_one("#answer-box").display = True
        markdown = self.query_one("#answer", Markdown)
        markdown.display = True
        await markdown.update(self.answer_text)
        parts = [
            result.model_used or "model unknown",
            _plural(len(result.citations), "source"),
        ]
        if result.cached:
            parts.append("cached")
        parts.append(f"{result.duration_ms / 1000:.1f} s")
        self._set_meta(" · ".join(parts))

    async def _hide_answer(self) -> None:
        self.answer_text = ""
        self.query_one("#answer-box").display = False
        markdown = self.query_one("#answer", Markdown)
        markdown.display = False
        await markdown.update("")

    def _show_sources(self, views: list[SourceView]) -> None:
        self.sources = views
        listing = self.query_one("#source-list", OptionList)
        listing.clear_options()
        listing.add_options(
            # Text objects, not strings: a string prompt is parsed as markup.
            Option(Text(self._row(view)), id=str(index))
            for index, view in enumerate(views)
        )
        if views:
            listing.highlighted = 0
        self._show_preview(0)
        self.refresh_bindings()

    @staticmethod
    def _row(view: SourceView) -> str:
        score = "n/a" if view.score is None else f"{view.score:.2f}"
        return f"{truncate(view.title, 38)}  {score}"

    def _show_preview(self, index: int) -> None:
        if not 0 <= index < len(self.sources):
            self.preview_text = _NO_SOURCES
        else:
            view = self.sources[index]
            score = "n/a" if view.score is None else f"{view.score:.3f}"
            header = f"{view.title}\n{view.chunk_ref} · score {score}"
            if view.document_id:
                header += f" · {view.document_id}"
            self.preview_text = f"{header}\n\n{view.text}"
        self.query_one("#source-preview", Static).update(self.preview_text)

    @on(OptionList.OptionHighlighted, "#source-list")
    def _on_source_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self._show_preview(event.option_index)
        self.refresh_bindings()
