# Terminal UI — Follow-ups

**Source:** the TUI work in PR #75 (merged), its plan
([`2026-10-01-tui.md`](2026-10-01-tui.md)) and design
([`../specs/2026-10-01-tui-design.md`](../specs/2026-10-01-tui-design.md)), and
the manual run against a real API server.

Every item below was raised during that work. The open decisions have been
made (see *Decisions*); nothing here is started. Items still marked
**(decision)** need an owner's call before anyone writes code. Each numbered
group is intended to be its own PR, one task per commit, per `CLAUDE.md`.

---

## Decisions (2026-10-02)

| Item | Decision |
|---|---|
| **A3** `/health` and Redis | **No**: `/health` must not depend on Redis. See A3 below for what that means in code. |
| **B6** Shared client package | **Yes**: do it. |
| **B7** Clients read `.env` | **Yes**: do it. |
| **C1, C2** | Verified by the repo owner. |
| **C3** | Verified on Linux only. Other terminals, macOS and Windows remain open. |

---

## A. Server changes the TUI is waiting on

- [ ] **A1. Populate document counts and tags** (own PR, as agreed).
  `GET /documents` never fills `tag_count` or `chunk_count`, and
  `GET /documents/{id}` never fills `tags`; the schema fields exist and are
  always `0` / empty today.
  - Compute the counts without an N+1: grouped subqueries over the chunk and
    tag tables, applied to the same page of documents.
  - Tests with documents that have, and lack, each of tags and chunks.
  - Then, in the TUI: add Tags and Chunks columns to the Documents table, and a
    Tags row to the detail modal (it currently has no row for them).
- [x] **A2. Use `X-Session-Id` on the server** (own PR). **Done** in
  `grimoire/api/session_id.py`; the MLflow tag is best effort (see the design
  spec's known limitations). OTel: the server has no OTel code today, so there
  was nothing to tag.
  The TUI already sends a per-launch id; a real server receives it and
  ignores it (confirmed in the manual run).
  - A pure-ASGI middleware in the style of `ContentLengthGuard` in
    `grimoire/api/main.py`, **not** `BaseHTTPMiddleware`.
  - Validate against `^[A-Za-z0-9_-]{1,64}$` using `fullmatch` (not `$`, which
    admits a trailing newline); ignore an invalid value rather than reject.
  - Bind it with `logger.contextualize(session_id=...)` and add
    `{extra[session_id]}` to the log format (neither loguru's default nor
    `DEFAULT_LOG_FORMAT` includes `extra`).
  - Tag MLflow / OTel runs with it where the server already creates them
    (the `CLAUDE.md` traceability requirement).
- [ ] **A3. Stop `/health` depending on Redis.** **Decided: no dependency.**
  The handler is trivial, but `@limiter.limit("60/minute")` puts the
  Redis-backed rate limiter in front of it, so with Redis down `/health` returns
  500 and the TUI's status bar reads a running API as *unreachable*.
  - Narrowest fix: exempt `/health` from the limiter (slowapi's
    `@limiter.exempt`). A liveness probe polled by Docker and by the TUI is not
    something to rate-limit anyway. Making the whole limiter fail open
    (`swallow_errors=True`) is a broader behaviour change and is **not** what was
    decided; raise it separately if wanted.
  - Test: with the limiter's storage unreachable, `GET /health` still returns
    200 and other limited routes behave as they do today.
  - Then update the README's Terminal UI section, which currently tells people
    to check Redis when the indicator says *unreachable*, and the matching line
    under "Known limitations" in the design spec.
  - Note for the reader: this was recorded as a yes/no on "should `/health`
    depend on Redis?", read as *no*. If "no" was meant as "leave it alone", say
    so and this item becomes a won't-do.

## B. TUI and client improvements

- [ ] **B1. Desktop GUI sends `X-Session-Id` too.** `GuiConfig.session_id`
  and the client header already exist; the GUI simply never sets one. Generate
  one per launch in `grimoire/gui/__main__.py`. Do this after A2 so it has an
  effect.
- [ ] **B2. In-app API key entry (v2).** A `Ctrl+K` modal, key held in memory
  only and never written to disk, as in the GUI's connection bar. Deliberately
  deferred from v1; for now the key comes from `GRIMOIRE_API_KEY`.
- [ ] **B3. Narrow terminals.** At exactly 80 columns a long title can clip the
  Added column and the table scrolls horizontally. Consider adaptive column
  widths, or dropping Size below a width threshold.
- [ ] **B4. Make the TUI test suite faster.** The 352 tests in `tests/tui` take
  several minutes (the Documents pane tests alone about 90 s; with the GUI and CLI
  tests, about four) because each starts an app. Look at fewer settle pauses, shared app fixtures, or marking
  the slow tail.
- [ ] **B5. Pin Textual honestly.** The floor is `>=8.2,<9` but only 8.2.8 was
  ever tested. Test the lowest 8.2.x, or raise the floor to what was tested,
  and consider a CI matrix.
- [ ] **B6. Move the shared client to a neutral package.** **Decided: yes.**
  The TUI imports `GrimoireClient`, `GuiConfig` and `GuiError` from
  `grimoire/gui/`. They are Qt-free, so it works, but they belong in a
  `grimoire/client/` package that neither front end owns.
  - Do it as a pure move first (own commit), with re-export shims left in
    `grimoire/gui/{client,config,errors}.py` so no GUI module or test needs to
    change in the same commit. Then switch the TUI and, separately, the GUI over
    to the new import path.
  - Settle the names when starting: keep `GuiConfig` / `GuiError` and add
    neutral aliases, or rename to `ClientConfig` / `ClientError` and alias the
    old names. Renaming touches about eight GUI modules and six GUI tests, so
    the alias-first route is lower risk.
  - Keep the import-hygiene tests: `grimoire.client` must stay free of Qt and
    the pipeline, and `tests/test_gui_config.py`'s check that the supported
    extensions match the parser must keep working.
  - **Do this before B7**: B7 changes `GuiConfig.from_env`, which B6 moves.
- [ ] **B7. Clients read `.env`.** **Decided: yes.** Neither `grimoire-gui` nor
  `grimoire-tui` does today; they read the process environment only (a README
  line claiming otherwise was corrected). Make both read a `.env` file for
  `GRIMOIRE_API_URL` and `GRIMOIRE_API_KEY`.
  - Precedence: a real environment variable beats `.env`, which beats the
    default. State it in the docs.
  - Read only those two keys, and never export `.env` into `os.environ` or log
    a value. `python-dotenv` (`dotenv_values`) is a hard requirement of
    `pydantic-settings` (a core dependency, which the server uses for the same
    file), so it is always installed; consider declaring it directly so the
    client does not rely on a transitive dependency.
  - Keep `GuiConfig.from_env` free of Qt and of any pipeline import, and keep
    it testable with an explicit mapping; give it an explicit `dotenv_path`
    argument so tests never depend on the current directory.
  - Decide and document where it looks (the current directory, as the server
    does, is the obvious answer).
  - Update `README.md` (GUI and TUI sections, which both say to
    `set -a; source .env; set +a`), `.env.example` (its comment says nothing
    loads `.env` for the clients), and `docs/superpowers/specs/2026-10-01-tui-design.md`
    (Configuration section).
  - Tests: precedence both ways, a missing file, a malformed file, a blank
    value counting as absent (as today), and that a key in `.env` is never
    logged.

## C. Verification that still needs a person with the real stack

The manual run used the real FastAPI app on SQLite with Redis, with only the
query agent substituted. These were **not** exercised:

- [x] **C1.** *(verified by the repo owner)* Ask and Search against real retrieval (embeddings, vector store,
  LLM). Check that answers, sources, scores and filters look right.
- [x] **C2.** *(verified by the repo owner)* The Docker Compose stack, and PostgreSQL (SQLite was used).
- [ ] **C3.** Real terminal emulators and other platforms. **Linux is
  verified** (by the repo owner); still open: macOS, Windows and Windows
  Terminal, tmux/screen, iTerm2. Only Linux and a pseudo-terminal have been
  exercised.

## D. Features deferred or out of scope for v1

Not committed to; listed so the decision is visible.

- [ ] Ingest, including a file picker (drag-and-drop does not exist in a
  terminal).
- [ ] Categories and tags management.
- [ ] Content generation, watcher control, wiki compilation.
- [ ] Delete a document.
- [ ] Free-text document search. The documents API has no title search or
  category/tag filter, so this needs a server change first.
- [ ] Theming beyond Textual's built-in themes.
- [ ] Snapshot tests (`pytest-textual-snapshot`); skipped as flaky.
- [ ] An in-process (serverless) mode that works without the API running.
  Needs the agents loaded locally, which gives up the TUI's instant start.

## E. Documentation

- [ ] **E1. Stale-image troubleshooting in `docs/deploy/docker.md`.** A
  contributor hit `api.upload_dir: Extra inputs are not permitted` from
  `grimoire-db-migrate` after pulling this branch. Cause: the image is built
  with a non-editable install, so a container keeps running the settings model
  it was built with while `docker-compose.yml` already sets the newer variable.
  Add an entry covering:
  - rebuild after pulling changes that touch settings:
    `docker compose up -d --build --force-recreate`;
  - the GPU overlay builds a **separate** image (`grimoire:gpu`), so rebuild
    the file set you actually start with, and check with
    `docker inspect <container> --format '{{.Config.Image}}'` and
    `docker images grimoire`;
  - never start the overlay without the base file (the file already warns about
    this).

## F. Pre-existing problems found along the way

None of these come from the TUI; they were found while running the full gate
and are recorded so they are not lost. Several overlap
[`../../FUTURE_TODO.md`](../../FUTURE_TODO.md).

- [ ] **F1. Six tests fail at the base commit** (identically before and after
  the TUI):
  - `tests/deploy/test_dockerfile.py::TestTorchVariant::test_torch_version_matches_the_lockfile`
  - `tests/test_mcp_mlflow.py::test_trace_mcp_tool_wraps_when_active`
  - `tests/test_parser.py::TestDocumentParserSupportedFormats::test_unsupported_txt`
  - `tests/test_parser.py::TestDocumentParserAsync::test_parse_unsupported_format`
  - `tests/test_storage_gdrive.py::TestGoogleDriveAdapterHappyPath::test_save_tokens`
  - `tests/test_storage_onedrive.py::TestTokenPersistence::test_tokens_saved_after_authentication`

  Some may be environment-related (Docling and the full dependency set were not
  installed where this was run); triage which are real.
- [ ] **F2. `pre-commit run --all-files` fails on the repository as it stands.**
  ruff reports 174 findings, black wants to reformat five files
  (`grimoire/cli/status.py` and four under `tests/deploy/`), bandit reports 8
  issues, and the mypy hook (in its isolated environment) reports errors.
  Decide whether pre-commit is meant to be a gate; if so, clear the backlog.
- [ ] **F3. E402 in `grimoire/cli/main.py`.** Already item 2 of
  `FUTURE_TODO.md`: add a `per-file-ignores` entry for the file instead of the
  per-line `# noqa: E402` the TUI's `tui` import carries. Once that lands the
  `noqa` can go.
- [ ] **F4. Three `S108` findings in `tests/test_cli.py`** (hardcoded `/tmp`
  paths). Use `tmp_path`.
- [ ] **F5. `ResourceWarning: unclosed database` from
  `TestStatusCommand`** (sqlite connections left open by the cache layer in
  those tests).
- [ ] **F6. Housekeeping note.** Running black over `tests/test_cli.py` while
  adding the TUI tests also reformatted 11 lines of existing code there
  (formatting only, in the direction the formatter wants). Harmless; mentioned
  so a reviewer is not surprised by the diff.

---

## Suggested order

Revised after the 2026-10-02 decisions.

1. **A3**: small and contained, and it removes a misleading "unreachable".
2. **A2**, then **B1**: the server-side session id, then the GUI sending one.
3. **B6**, then **B7**: the shared package first, because B7 changes code that
   B6 moves.
4. **A1**: unblocks the richer Documents table and detail view.
5. **E1**: cheap, and it has already cost someone time.
6. **C3** (macOS, Windows, other terminals) as access allows.
7. Everything else as it becomes useful.
