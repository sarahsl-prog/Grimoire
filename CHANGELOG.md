# Changelog

All notable changes to Grimoire are documented in this file.

## [Unreleased]

### Added

- **Document search.** `GET /api/v1/documents` takes a `q` parameter: a
  case-insensitive substring match on the title or source path, composable with
  the existing filters and pagination (`%` and `_` in the text match literally).
  The shared client's `list_documents` accepts `q`, and the TUI's Documents pane
  has a search box (`/` to focus, `Enter` to apply, `Esc` to clear).

## [2.1.0] - 2026-10-04

### Security

- `grimoire mcp --sse` now binds `127.0.0.1` by default instead of `0.0.0.0`.
  Pass `--host 0.0.0.0` to expose it. The `mcp` compose service is unaffected: it
  runs uvicorn with its own `--host`.

- The `/ingest` path allowlist is now `/tmp` only. It previously also allowed
  a hardcoded `/home/sunds`. Ingest files from `/tmp` or use the upload endpoint.

- The standalone MCP SSE transport (`grimoire mcp --sse`) now requires an
  `X-API-Key` header, matching the API-server-mounted transport. Previously
  it served every MCP tool — including `grimoire_ingest_file`,
  `grimoire_pg_query`, and `grimoire_delete_document` — with no
  authentication at all, so any client able to reach the port could read or
  modify the full corpus. Update any client still pointed at the old
  unauthenticated `/sse` path.

### Added

- An `all` extra: `uv sync --extra all` (or `pip install grimoire[all]`) installs
  every optional group (`mlflow`, `gui`, `tui`, `dev`). `uv sync --all-extras`
  does the same.
- **Terminal UI** (`grimoire-tui`, `grimoire --tui`, `grimoire tui`;
  `uv sync --extra tui`) – a Textual client for the REST API with Search/Ask
  (the answer beside its source chunks, metadata filters, `Esc` to abandon a
  slow query) and Documents (a paged table filterable by status and file type,
  with a detail view). It is a thin HTTP client and never imports the
  ingestion pipeline. It logs to `./logs/grimoire-tui.log` only, never to the
  terminal, and sends a per-launch `X-Session-Id` header so server-side
  records can be matched to it.
  - `GrimoireClient` gains `list_documents`, `get_document`, and an optional
    `filter_dict` on `ask`/`search`; `GuiConfig` gains an optional validated
    `session_id`. The desktop GUI's requests are unchanged.
- **Desktop GUI** (`grimoire-gui`, `uv sync --extra gui`) – a PySide6 client
  with Search/Ask (including source chunk inspection), recent ingests,
  drag-and-drop ingest, and a read-only CLI runner. It is a thin HTTP client
  over the REST API and never imports the ingestion pipeline.
- `POST /api/v1/ingest/upload` – multipart upload that stages the file
  server side, so ingestion no longer requires a shared filesystem between
  the client and the API.
  - New `GRIMOIRE_API__UPLOAD_DIR` and `GRIMOIRE_API__MAX_UPLOAD_BYTES`
    settings, and an `app_uploads` named volume in `docker-compose.yml` to
    persist staged uploads across restarts.
- **Container image and Compose services** – `Dockerfile` builds a
  multi-stage `grimoire:latest` image (CPU torch by default); `api`,
  `mcp`, `watcher`, and a one-shot `db-migrate` service run it in
  `docker-compose.yml` alongside Postgres, Redis, and ChromaDB.
  - `docker-compose.gpu.yml` — rebuilds the image with the CUDA torch
    variant as `grimoire:gpu` and reserves a GPU device.
  - `docker-compose.dev.yml` — bind-mounts the source tree over the
    installed package and enables uvicorn's `--reload`.
  - See [docs/deploy/docker.md](docs/deploy/docker.md) for the full guide.

### Changed

- The standalone SSE endpoint moved from `/sse` to `/mcp/sse`, matching the
  path the API server has always used.
- Containerized deployments use the `chromadb` service over HTTP instead of
  an embedded (in-process) ChromaDB client.
- The API and MCP apps now report `grimoire.__version__` instead of a
  hard-coded string, so the version is defined once.
- `uv.lock` is now tracked (the Dockerfile copies it).

### Fixed

- `grimoire status --detailed`, `cache stats` and `cache clear` now close the
  disk cache they open instead of leaving a SQLite connection to the garbage
  collector.

### Documentation

- Added [`docs/UPGRADING.md`](docs/UPGRADING.md) — a full in-place upgrade
  procedure for existing installs (backups, dependency sync, configuration
  drift, Alembic migrations, cache invalidation, re-embedding, rollback, and
  troubleshooting), plus a condensed `Upgrading an existing install` section
  in the README. Per-release `Upgrading` blocks below remain the source of
  truth for version-specific steps.
- Added [`docs/deploy/docker.md`](docs/deploy/docker.md) — the general
  application deployment guide for the container image and Compose stack
  (quick start, services, configuration precedence, Ollama, GPU/dev
  overlays, volumes and backup, troubleshooting).

## [2.0.0] - 2026-06-18

### Added

- **MCP server** – Grimoire now exposes its full knowledge-base functionality via the Model Context Protocol (MCP) so AI assistants can query and manage documents natively.
  - `stdio` transport for local clients such as Claude Desktop, Cursor, and Windsurf.
  - `SSE` transport mounted at `/mcp` inside the existing FastAPI API server, plus a standalone `grimoire mcp --sse` server.
  - 17 tier-gated tools: `grimoire_search`, `grimoire_search_cve`, `grimoire_search_playbook`, `grimoire_ask`, `grimoire_get_document`, `grimoire_list_documents`, `grimoire_list_categories`, `grimoire_watch_status`, `grimoire_status`, `grimoire_ingest_file`, `grimoire_ingest_directory`, `grimoire_generate`, `grimoire_create_category`, `grimoire_watch_start`, `grimoire_watch_stop`, `grimoire_pg_query`, and `grimoire_delete_document`.
  - Security-domain search: `grimoire_search_cve` (exact CVE lookup or semantic search over NVD CVEs with severity/CVSS/year facets) and `grimoire_search_playbook` (semantic search over Sigma detection rules **and** native playbook documents with MITRE technique, platform, log-source, IR-phase, and severity facets).
- **Playbook corpus** – New `SourceType.PLAYBOOK` with deterministic detection (path hints `/playbooks/`, `/runbooks/`, `/ir-playbooks/`, `/response-plans/`; front-matter keys `playbook:`/`phase:`/`trigger:`; canonical `## Trigger` + `## Actions` section structure). Playbooks parse front matter into `SecurityMetadata` playbook facets (`playbook_phase`, `action_type`, `trigger`) and chunk one-per-section.
  - `grimoire ingest --source-type playbook` override supported; `docs list / search --source-type playbook` accepted in CLI and API.
  - `grimoire_search_playbook` gained `source_types` (`all` default = playbooks + sigma), and `phase` facet for IR-phase filtering.
  - No DB migration — playbook fields ride the existing `documents.security_metadata` JSONB blob.
  - Tier-based access control:
    - `rdl` (Read) – search, search_cve, search_playbook, ask, get/list docs/categories, watch_status, status.
    - `dvl` (Dev) – Read + ingest, generate, create_category, watch_start, watch_stop, pg_query.
    - `agt` (Agent) – Dev + delete_document.
- New CLI command: `grimoire mcp [--stdio|--sse --host HOST --port PORT]`.
- New CLI command group: `grimoire key create|list|revoke` for API-key management.

### Changed

- FastAPI app version synchronized to `2.0.0`.
- `pyproject.toml` now depends on `mcp>=1.8.0`.
- SSE MCP requests require an `X-API-Key` header validated by middleware before proxying to the MCP app.
- stdio MCP sessions require a valid `GRIMOIRE_API_KEY` environment variable validated at startup.

### Security

- `grimoire_pg_query` now wraps user-supplied SQL in a subquery and applies a bound `LIMIT` parameter using SQLAlchemy; non-SELECT/WITH statements and `SELECT INTO` are rejected by a Pydantic validator.
- MCP auth reuses the existing bcrypt-hashed API key system.

### Upgrading

1. Pull the new dependencies:
   ```bash
   uv sync
   ```
2. Apply any pending database migrations:
   ```bash
   uv run alembic upgrade head
   ```
3. Create an API key for your MCP client:
   ```bash
   # For full access (recommended for admin/Claude Desktop)
   grimoire key create --tier agent --name claude-desktop

   # For read-only assistants
   grimoire key create --tier read --name read-only-bot
   ```
4. Start the MCP server:
   - stdio:
     ```bash
     GRIMOIRE_API_KEY=grim_agt_... uv run grimoire mcp --stdio
     ```
   - standalone SSE:
     ```bash
     uv run grimoire mcp --sse --port 8100
     ```
   - SSE via the API server (already mounted at `/mcp`):
     ```bash
     uv run uvicorn grimoire.api.main:app --port 8001
     ```

See `README.md` for sample Claude Desktop / Cursor configurations.

## [1.x.x] - Previous releases

- Pre-2.0 history is available in the Git commit log.
