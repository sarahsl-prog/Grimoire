# Grimoire Desktop GUI — Design

**Date:** 2026-09-21
**Status:** Approved for planning
**Scope:** A PySide6 desktop client with four tabs (Search/Ask, Recent ingests,
Drag-and-drop ingest, CLI runner), plus the one API endpoint the drop zone
needs.

---

## Problem

Every way into Grimoire today is a terminal or an editor. `grimoire ask`
prints an answer, `grimoire ingest` takes a path, and the MCP server serves
Claude and Cursor. Three things are awkward as a result:

1. **Retrieval is hard to sanity-check.** An answer that reads well can rest on
   the wrong chunks. Seeing the answer and its source chunks side by side, and
   being able to run retrieval *without* generation, is a diagnostic the CLI
   makes tedious.
2. **Ingesting a file means typing its path.** The common case — a PDF that just
   landed in `~/Downloads` — costs a copy-paste of an absolute path into a
   command.
3. **No at-a-glance view of what the corpus just absorbed.** Confirming that an
   ingest produced chunks means another command and reading a table in the
   scrollback.

A small desktop client fixes all three without changing how the corpus works.

## Non-Goals

- Replacing the CLI or the API. The GUI is a client, and a partial one.
- Document editing, deletion, or detail views; category and tag management;
  wiki compilation; content generation; watcher control. All reachable by CLI
  and API, none in this scope.
- Multi-user support, remote access, or authentication beyond passing the
  existing API key.
- Packaging as a standalone binary (PyInstaller, AppImage). Installation is
  `uv sync --extra gui`.
- Theming beyond the Qt platform default.
- Running the GUI in a container. See *Deployment*.

## Prerequisite

Qt needs a display server. On WSL2 that means **WSLg**, which ships with
Windows 11 and recent Windows 10 builds. Verify before starting implementation:

```bash
echo "$DISPLAY $WAYLAND_DISPLAY"   # expect :0 and wayland-0
```

If both are empty, the GUI cannot draw and no amount of application code will
change that. This is an environment prerequisite, not an implementation task.

---

## Architecture

The GUI is a thin HTTP client over the existing REST API. It imports no
pipeline code, so the GUI process never loads torch, Docling, or ChromaDB.

```
host                                   containers (or bare metal)
┌──────────────────┐                   ┌─────────────────────┐
│  grimoire-gui    │ ── HTTP ────────▶ │  grimoire-api :8001 │
│  (PySide6)       │   X-API-Key       └──────────┬──────────┘
│                  │                              │
│  CLI runner tab  │ ── QProcess ──▶ grimoire     │  IngestionAgent
└──────────────────┘    (local)                   │  QueryAgent
                                                  ▼
                                       Postgres · Chroma · Ollama
```

### Why the REST API and not MCP

Both transports are thin skins over the same agents: `grimoire_ask`
(`grimoire/mcp/tools.py:710`) and `POST /api/v1/query/ask`
(`grimoire/api/routes/query.py:68`) both call `QueryAgent.query()` against the
same database pool, vector store, cache, and Ollama instance. Neither is
faster in any way that matters — latency is dominated by embedding and LLM
generation, and transport framing is milliseconds against seconds.

The API wins on fit, not speed:

| | REST API | MCP |
|---|---|---|
| Response shape | typed Pydantic models the client imports | JSON string inside `{"status":"ok","data":…}` (`grimoire/mcp/tools.py:42`) |
| File upload | multipart is addable | not expressible; `grimoire_ingest_file` takes a server-side path |
| Client cost | one `httpx.Client` | session handshake, tool discovery, an SSE stream to hold open and reconnect |
| Errors | HTTP status codes | error string inside a 200 response |
| Intended consumer | programs | language models |

Choosing MCP would mean writing an MCP client inside a Qt application to reach
functions that already expose a typed HTTP interface, and would still leave
drag-and-drop ingest unsolved.

### Module layout

A new subpackage, following the existing one-package-per-feature convention:

```
grimoire/gui/
  __init__.py
  __main__.py          # console script entry point: grimoire-gui
  app.py               # QApplication bootstrap, MainWindow, QTabWidget
  config.py            # GuiConfig — base URL, API key, timeouts, size cap
  client.py            # GrimoireClient — sync httpx.Client, typed methods
  errors.py            # GuiError hierarchy
  workers.py           # ApiWorker(QRunnable) + WorkerSignals
  widgets/
    __init__.py
    connection_bar.py  # base URL, key state, health indicator
    citation_card.py   # one source chunk, reused by both query modes
    search_tab.py
    recent_tab.py
    ingest_tab.py
    cli_tab.py
```

`client.py` parses responses with the server's own models from
`grimoire.api.schemas` rather than redeclaring them. That module imports only
Pydantic, so it costs the GUI nothing, and it makes a server-side schema change
a type error in the client instead of a runtime surprise.

### Dependencies

PySide6 goes in a new optional-dependency group so the server image and CI stay
Qt-free:

```toml
[project.optional-dependencies]
gui = ["PySide6>=6.7"]

[project.scripts]
grimoire-gui = "grimoire.gui.__main__:main"
```

`pytest-qt` joins the `dev` group. Installation is `uv sync --extra gui`;
`uv sync` alone continues to produce a headless environment.

PySide6 ships type stubs, so `mypy grimoire/gui/` under the project's strict
settings is expected to pass. Should a specific stub prove unusable, the
narrowest possible `# type: ignore[code]` with a comment naming the cause is
acceptable — a blanket module-level exclusion is not.

---

## Concurrency

Qt's event loop is synchronous; the API is slow. One rule governs the whole
design: **nothing that can block runs on the GUI thread.**

- **Tabs 1–3** submit an `ApiWorker(QRunnable)` to a `QThreadPool`. The worker
  calls one `GrimoireClient` method and emits `finished(result)` or
  `failed(GuiError)`. Widgets touch Qt objects only in those slots, which run on
  the GUI thread.
- **No asyncio anywhere in the GUI.** `httpx.Client`, not `AsyncClient`. Mixing
  an asyncio loop into a Qt application buys nothing here and costs a whole
  class of bugs.
- **Tab 4** uses `QProcess`, which is already non-blocking and emits
  `readyReadStandardOutput` as the child writes.
- **Timeouts:** connect 5s everywhere; read 300s for `/query/ask` and upload,
  30s for search and document listing.
- **No stacking.** A submit button disables itself while its request is in
  flight and re-enables in both the success and failure slot.
- **Shutdown.** `MainWindow.closeEvent` waits on the thread pool with a bounded
  timeout and kills any running `QProcess`, so the application does not leave
  orphans behind.

---

## Tabs

### 1. Search / Ask

A query `QLineEdit`, a segmented `Ask | Search` toggle, a `top_k` spin box
(1–100, matching the server's bounds), and a "use cache" checkbox.

**Ask** calls `POST /api/v1/query/ask`. The answer renders as markdown in a
`QTextBrowser` at the top; source chunks render below as citation cards, each
showing `document_title`, `relevance_score`, `content_snippet`, and a
copy-to-clipboard button for `document_id`. A footer line reports `model_used`,
`cached`, and `duration_ms`.

**Search** calls `POST /api/v1/query/search`. Cards only, no answer panel, no
LLM in the path. This is the fast retrieval check: it answers "did the right
chunks come back?" without waiting on generation.

Empty results show an explicit "no matches" state, not a blank panel.

### 2. Recent ingests

A `QTableWidget` fed by `GET /api/v1/documents?limit=10&offset=0`. The endpoint
already orders by `Document.created_at DESC`
(`grimoire/api/routes/documents.py:59`), so no client-side sorting is needed.

Columns map one-to-one onto `DocumentResponse` fields: Title, Type
(`file_type`), Status (`processing_status`), Chunks (`chunk_count`), Tags
(`tag_count`), Size (`size_bytes`, humanized), Created (`created_at`,
local time). Failed rows are visually distinct.

Refresh happens on tab activation, on an explicit Refresh button, and once when
the ingest tab signals a completed upload. **No `QTimer` polling.** Every API
key carries a per-minute rate limit (`grimoire/api/auth.py:37`), and a
background poll spends that budget on a window nobody is looking at.

### 3. Drag-and-drop ingest

A `QFrame` drop zone accepting file URLs, with a click-to-browse fallback via
`QFileDialog`.

Dropped files are filtered client-side before any request: extension against
the supported set, size against the configured cap. Rejected files appear in
the queue with the reason rather than vanishing. Accepted files upload one at a
time — the pipeline is CPU- and GPU-bound, so parallel uploads would queue on
the server anyway while making progress reporting meaningless.

A determinate `QProgressBar` tracks **file count**, not bytes. Byte-level
progress requires a streaming upload with a read callback, which is real
complexity for a local-network transfer that is not the slow part; parsing and
embedding are. Each queue row shows pending → uploading → ingesting →
done / skipped / failed, with the server's `error_message` on failure and the
`chunks_created` count on success. An `auto_tag` checkbox maps to the request
field.

On the first successful ingest of a batch the tab emits a signal the Recent
tab consumes.

### 4. CLI runner

A command `QComboBox`, an arguments `QLineEdit`, and Run / Stop buttons, with
output streaming into a read-only `QTextEdit`.

`QProcess` launches the `grimoire` executable resolved from `PATH`, falling
back to `sys.executable -m grimoire.cli.main`. **No shell.** Arguments are split
with `shlex.split` and passed as an argv list, so no amount of quoting in the
arguments field can chain a second command.

The subcommand is chosen from the dropdown, never typed, and the allowlist
holds read-only commands:

```
status · search · ask · docs · config show · cache stats · categories list
```

Deliberately excluded: `keys` (issues credentials), `migrate` (mutates schema),
`reindex` (long-running rewrite), `untag`, `watch` (starts a daemon outside the
GUI's lifecycle), `ingest` (tab 3 owns that path). These remain available in a
terminal, where their consequences are in front of the person running them.

Output appends with a 5000-line cap on the document block count, so a chatty
command cannot exhaust memory. On exit the tab prints the exit code. Stop sends
`terminate()`, then `kill()` after a two-second grace period.

---

## New endpoint: `POST /api/v1/ingest/upload`

The drop zone cannot use the existing `/ingest/file`: that endpoint takes a
server-side path and validates it against `_ALLOWED_ROOTS`, hardcoded to `/tmp`
and `/home/sunds` at `grimoire/api/routes/ingest.py:25`. A containerized API
does not share the host's filesystem, so a dropped path is meaningless to it.

**Contract.** Multipart `UploadFile`, authenticated with
`Depends(get_api_key)` like its siblings, `auto_tag` as an optional form field.
Returns the existing `IngestResultResponse` — no new response schema.

**Handling.**

1. Validate the extension against `DocumentParser.SUPPORTED_EXTENSIONS`
   (`grimoire/core/parser.py:149`) → `415` on mismatch.
2. Stream the body to disk in chunks, counting bytes, aborting and deleting the
   partial file past the cap → `413`. Never `await file.read()` in one shot: a
   large upload would otherwise sit in memory.
3. Write to `settings.api.upload_dir` under the name
   `{uuid4().hex}_{Path(filename).name}`. The client's filename contributes only
   a sanitized basename, so the path is server-determined — strictly safer than
   validating a client-supplied path, and the reason this endpoint needs no
   `_is_path_allowed` equivalent.
4. Call the existing `agent.ingest_file(db, staged_path, auto_tag=...)`.
5. Return its result.

**Retention.** The staged file is **kept**. `ingest_file` records the path it
was given as `Document.source_path`, and the local storage adapter reads files
in place rather than copying them, so deleting the upload would orphan the row.
The implementation plan carries an explicit task to confirm this against
`grimoire/agents/ingestion.py` before finalizing, since retention is the kind of
decision that is expensive to reverse once a corpus has been ingested through
it.

**Configuration.** A new `APIConfig` field:

```python
upload_dir: Path = Field(default=Path("uploads"), description="Staging directory for uploaded files")
max_upload_bytes: int = Field(default=100 * 1024 * 1024, ge=1, description="Maximum accepted upload size")
```

`APIConfig` sets `extra="forbid"`, so both fields must be declared rather than
passed through. The directory is created at startup if absent.

**Deployment.** Because the staging directory holds the only copy of every
uploaded document, it must survive container recreation. `docker-compose.yml`
gains a named volume on the shared `x-grimoire-app` anchor:

```yaml
volumes:
  - app_uploads:/app/uploads
environment:
  GRIMOIRE_API__UPLOAD_DIR: /app/uploads
```

with `app_uploads` declared alongside `app_logs` and `app_cache`. Omitting this
is data loss on the next `docker compose down -v`, so it belongs in the same
commit as the endpoint.

---

## Configuration

The GUI reads two variables at startup, from the environment or the project
`.env`:

| Variable | Default | Purpose |
|---|---|---|
| `GRIMOIRE_API_URL` | `http://localhost:8001` | API base URL. Matches `APIConfig.port` (`grimoire/config/settings.py:728`) and the published port in `docker-compose.yml:157`. |
| `GRIMOIRE_API_KEY` | none | Sent as `X-API-Key`. |

The connection bar shows the active base URL and whether a key is present, and
lets a key be pasted for the current session. **A key entered in the GUI is held
in memory only** — never written to `QSettings` or any config file, because a
plaintext credential in `~/.config` outside `.env` is a worse default than
retyping it.

With no key configured the window opens in an explicit "not configured" state
naming the two variables, rather than looking functional until the first click
returns 401.

---

## Error handling

`GrimoireClient` converts every failure into a `GuiError` carrying text meant
for a person:

| Condition | Message |
|---|---|
| connection refused / DNS failure | "Cannot reach the Grimoire API at `<url>` — is the stack running?" |
| timeout | "Request timed out after Ns." |
| 401 | "API key rejected." |
| 403 | "This API key's tier is not permitted to do that." |
| 404 | "Not found." |
| 413 | "File exceeds the NN MB limit." |
| 415 | "Unsupported file type: `.xyz`" |
| 422 | The server's validation detail, flattened to one line. |
| 429 | "Rate limited — retry in N seconds." (from `Retry-After`) |
| 5xx | "Grimoire API error — check the API logs." |
| unparseable body | "Unexpected response from the API." |

Raw exceptions and tracebacks go to `loguru` and never to a widget, per the
project's error-handling standard. A `session_id` (a UUID minted at launch) is
bound to the logger so a GUI session's records can be isolated, matching the
context-in-log-records convention.

Each tab shows its own inline error label; the status bar holds the most recent
error across tabs.

MLflow tracing is out of scope for the GUI: the server already traces the work
that matters, and the client contributes only a round trip.

---

## Testing

`pytest-qt` with `QT_QPA_PLATFORM=offscreen`, so the suite runs headless in CI
alongside the existing tests.

**Client** (`tests/test_gui_client.py`, using the project's existing
`pytest-httpx`): one test per row of the error table, plus successful parses of
`QueryResponse`, `SearchResponse`, `DocumentListResponse`, and
`IngestResultResponse`.

**Widgets** (`tests/test_gui_widgets.py`), driving real widgets against a stub
client:

- Search tab: ask renders answer plus cards; search renders cards only; empty
  results show the empty state; a failure shows the inline error and re-enables
  the button.
- Recent tab: ten rows populate correctly; a failed document is marked; refresh
  re-requests; activating the tab triggers exactly one request.
- Ingest tab: an unsupported extension is rejected before any request; an
  oversized file is rejected client-side; a mixed batch reports per-file
  outcomes; a completed batch emits the refresh signal.
- CLI tab: a non-zero exit code is surfaced; output streams incrementally;
  Stop terminates a running process; arguments containing shell metacharacters
  are passed through as literal argv entries and do not spawn anything.

**Endpoint** (added to the existing `tests/test_api.py` patterns): happy path;
over-cap upload rejected with 413 and no file left on disk; unsupported
extension rejected with 415; a filename shaped like `../../etc/passwd` lands in
the staging directory under a sanitized name; a missing API key returns 401.

---

## Risks

| Risk | Mitigation |
|---|---|
| No display server under WSL2 | Verified as a prerequisite before implementation starts, not discovered at first run. |
| PySide6 stubs fight `mypy --strict` | Narrow, commented `type: ignore` at the specific call; no module-level exclusion. |
| Qt in CI | `offscreen` platform plugin; the `gui` extra is installed only for the GUI test job. |
| Staged uploads lost on container recreation | Named volume in the same commit as the endpoint. |
| The GUI drifts from API schema changes | Client imports `grimoire.api.schemas` rather than duplicating models. |

---

## Definition of Done

- `grimoire-gui` launches from an environment built with `uv sync --extra gui`.
- All four tabs work against the containerized stack with no shared filesystem.
- `POST /api/v1/ingest/upload` documented in the README and the API docs.
- `docs/deploy/docker.md` records the `app_uploads` volume and
  `GRIMOIRE_API__UPLOAD_DIR`.
- `pre-commit run --all-files` clean; `mypy grimoire/` clean.
- Tests above pass, with the GUI suite green headless.
