# Grimoire Terminal UI — Design

**Date:** 2026-10-01
**Status:** Implemented (PR #75)
**Scope:** A Textual terminal client with two screens, Search/Ask and Documents,
launched with `grimoire-tui` (also `grimoire --tui` and `grimoire tui`).
**Plan:** [`2026-10-01-tui.md`](../plans/2026-10-01-tui.md)

---

## Problem

The desktop GUI needs a display server, which rules out SSH sessions and bare
servers, and the CLI prints an answer and forgets it. Two things are awkward as
a result:

1. **Retrieval is hard to sanity-check from a terminal.** An answer that reads
   well can rest on the wrong chunks. Seeing the answer and its source chunks
   side by side, and running retrieval *without* generation, is the diagnostic.
2. **Browsing the corpus means re-running `grimoire docs list` with different
   flags.** A paged, filterable table with a detail view is the natural shape.

## Non-Goals

- Replacing the CLI, the API or the desktop GUI. The TUI is a client, and a
  partial one.
- Ingest, categories and tags, generate, watch, wiki, delete, free-text document
  search, and watcher control. All reachable by CLI and API.
- Running without the API server. There is no in-process (serverless) mode.
- Multi-user support, remote access, or authentication beyond the existing API
  key.
- Saving an API key. In-app entry (`Ctrl+K`) exists, but the key is session-only.

## Architecture

The TUI is a thin synchronous HTTP client over the existing REST API. It uses
`GrimoireClient`, `ClientConfig` and `ClientError` from the shared
`grimoire/client/` package (all Qt-free; `ClientConfig`/`ClientError` are the
neutral names for the GUI's `GuiConfig`/`GuiError`), so the process never loads torch, Docling, ChromaDB or PySide6. A
subprocess test asserts this.

```
grimoire-tui / grimoire --tui / grimoire tui
        │
        ▼
grimoire/tui/__main__.py   args, Textual check, file-only logging, config, client
        │
        ▼
GrimoireApp ── StatusBar (address · key set? · connected/unreachable)
   │      └── TabbedContent
   │             ├── SearchPane ──────┐
   │             └── DocumentsPane ───┤── DocumentDetailScreen (modal)
   ▼                                  ▼
 health worker                  thread workers → GrimoireClient → REST API
```

Every client call runs in a Textual thread worker and reports back by message.
A thread cannot be killed, so a superseded request still finishes; each pane
guards against its late result with two independent checks (the worker's
`is_cancelled` flag, and a request id compared in the handler).

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Transport | HTTP client only | Starts instantly; works against bare-metal or the containerized stack; reuses a tested client |
| Launcher | `grimoire-tui` canonical; `--tui` and `tui` are shortcuts | The CLI group's callback validates server settings and installs a stderr log sink, both wrong for a full-screen client. The flag is *eager* and the subcommand returns early, so neither runs them |
| Framework | Textual, as an optional extra (`tui`) | Async-native, tabs/tables/markdown built in, a headless test pilot |
| Logging | File only (`./logs/grimoire-tui.log`) | Any line on stderr paints over the screen. stdlib `logging` (httpx) is routed into the same file |
| Correlation | Per-launch session id, sent as `X-Session-Id` and written on every log line | Lets a server-side record be matched to the TUI log. Server-side handling is a follow-up PR |
| API key | `GRIMOIRE_API_KEY` from the environment only | A command-line key leaks into `ps` and shell history. `Ctrl+K` opens an in-memory-only entry modal |
| Untrusted text | Never parsed as markup | Titles and chunks come from ingested files, which in the security corpus may be hostile |

## Screens

**Search / Ask.** A query box, an Ask/Search toggle, a top-k box (rejected, not
clamped, outside 1 to 100), and a collapsed filter row (tags, source type,
severity, CVE id). Ask shows the answer (Markdown, links never opened) and a
line with model, source count and time. Sources are a list beside a preview of
the highlighted chunk. `Esc` abandons a running request.

**Documents.** Nothing is fetched until the tab is first shown. A table
(Title, Type, Status, Chunks, Tags, Size, Added) of 50 rows per page, newest first, filterable
by status and file type. `]` and `[` page; a page past the end falls back to the
last real page. A reload keeps the cursor on the same document.

**Document detail.** A modal with every field, including the full source path,
the chunk count, the tag names and, for a failed document, the error message. A failed fetch is shown inside
the modal and does not close it.

### Keys

| Key | Action |
|---|---|
| `F1` / `F2` | Search / Ask, Documents |
| `Ctrl+R` | Re-check the API; reload the current Documents page |
| `Ctrl+K` | Enter or replace the API key (masked, in memory only) |
| `?`, `Ctrl+P` | Key help panel, command palette |
| `Ctrl+Q` | Quit |
| `Esc` | Abandon a running query; close the detail view |
| `o` / `Enter` | Open a source's document |
| `]` / `[` | Next / previous Documents page |
| `Enter` | Open the document under the cursor |
| `q` | Close the detail view |

## Configuration

`GRIMOIRE_API_URL` (default `http://localhost:8001`) and `GRIMOIRE_API_KEY`,
read per key from the process environment first, then a `.env` file in the
current directory (only those two keys; nothing is exported, no interpolation,
a missing or unreadable file is ignored), then the default. `--url` overrides
all of them; `--debug` adds DEBUG records to the log. A malformed URL is rejected at
startup (`httpx` accepts almost any string and would only fail on the first
request).

## Error handling

The client raises only `ClientError` subclasses, each carrying a complete
sentence written for a person. A pane shows that sentence, or a generic line
(with the traceback in the log) for anything unexpected. Raw exception text
never reaches the screen. The status bar is kept honest between health checks:
a connection failure marks the API unreachable; any other client error (a 401,
429 or 500 is still an answer) marks it reachable; a timeout proves nothing.

## Testing

352 tests in `tests/tui`, all headless (`App.run_test()`), against a scriptable stub
client plus a handful against the real client and a local socket. Beyond the
usual cases the suite pins the failure modes a thread-based UI gets wrong:
stale responses, double submits, hostile text in every widget, and focus
handling across tab switches. Mutation-checking (breaking the code on purpose
and confirming a specific test fails) was used throughout.

The implementation was also exercised against the **real** FastAPI app on
SQLite with a real API key and Redis, with only the query agent substituted
(no embedding model or LLM was available). That run found the long-title column
overflow, confirmed the `X-Session-Id` header reaches a real server, and
confirmed the terminal is restored after quitting.

## Known limitations

- Tag and chunk counts (and the detail modal's tag names) come from the API as
  of follow-up A1. Against an older server they read `0` / `-`, because the
  fields default to empty there.
- The server does not yet use `X-Session-Id`. Follow-up PR B.
- The Documents table never shows tag or chunk counts: `GET /documents` does
  not populate them. Follow-up PR A.
- The server logs `X-Session-Id` (follow-up A2). The MLflow tag on MCP traces is
  best effort: it relies on the id being in the tool call's context, which was
  not verified over the SSE transport.
- Narrow terminals: the Documents table plans its columns from the terminal's
  width (`grimoire/tui/layout.py`) and re-plans when it is resized. The title
  shrinks first, to a floor of 24 characters; then Tags, Chunks, Type and Size
  are dropped in that order. Title, Status and Added are never dropped, so Added
  is never scrolled off screen. The range line under the table says which
  columns are hidden. Below roughly 55 columns even the essentials cannot fit and
  the table scrolls sideways. Widths were verified at 60 to 200 columns; the
  planner is pure and unit-tested, and the pane is tested through a resize.
- The connected/unreachable indicator uses `GET /health`. Originally that
  needed Redis (the API's rate limiter), so a running API with Redis down read
  as unreachable; `/health` now has its own in-memory limit and no longer does.
- Ask and Search against the real retrieval stack (embeddings, vector store,
  LLM) were not exercised; only the API contract and the client were.

## Deferred to v2

Ingest, categories and tags,
free-text document search.

## API key entry (added after v1)

`Ctrl+K` opens a modal with a masked input. The key is validated (printable ASCII,
no spaces, at most 256 characters; a bad value is explained without echoing it),
then applied by building a **new** `GrimoireClient` from the current config plus
the key, because the key is a default header fixed when a client is built. This
mirrors the desktop GUI. The old client is retired, not closed, until exit (a
worker may still hold it); the app closes the clients it built and never the one
it was given. The panes switch clients, the status bar reads `key: set`, the API
is re-checked and the visible pane reloads, since the request that prompted the
new key probably failed. If the new client cannot be built, the old client and
config are kept. The key is never written, logged, shown or put in a message.
The binding has priority because a focused `Input` otherwise binds `Ctrl+K` to
delete-to-end-of-line.

## Textual support

`pyproject.toml` allows `textual>=8.2,<9`. The floor was originally a guess: only
8.2.8 had ever been run. The full suite (517 tests) has since been run against
every release from 8.2.0 to 8.2.8 (nine versions) and passes on each, so the
floor is now a tested one. The upper bound is not: `<9` trusts Textual to keep
its API within a major version, and no release past 8.2.8 existed to test. If a
newer 8.x breaks the TUI, narrowing the bound to `<8.3` is the conservative fix.

The product code uses no private Textual attribute. The tests do use a few
(`app._notifications`), so a new Textual can break a test without breaking the
product.

`scripts/test-textual-versions.sh` reproduces the check: it installs each version
into a throwaway directory ahead of the active environment on `PYTHONPATH` (so
the environment itself is not touched), confirms the right version was imported,
and runs `tests/tui`. `tests/tui/test_textual_support.py` keeps the declared range,
the two copies of it in `pyproject.toml` (the `tui` and `dev` extras), and the
script's list of tested versions from drifting apart. There is no CI
configuration in the repository, so the matrix is run by hand.
