# Changelog

All notable changes to Grimoire are documented in this file.

## [Unreleased]

### Security

- **Deleted documents can no longer surface in search through orphaned
  vectors.** Search results come from the vector store, which holds chunk
  text; if vector cleanup failed after a delete, the deleted document's text
  was still returned by search and fed to RAG answers. Hybrid search now drops
  vector hits whose document no longer exists (one batched query), failing
  closed if that check errors.
- **One delete contract for REST, MCP and the watcher.** All three now delete
  and commit the document's rows first, then remove its vectors via
  `grimoire.vectorstore.cleanup.purge_document_vectors`, which never raises and
  logs any orphaned vector ids. Previously the REST route deleted vectors first
  and returned a raw 500 when ChromaDB was down; the MCP tool reported an error
  for a delete that had succeeded (it now returns `vectors_removed: false`).
- **`grimoire_pg_query` row limit can no longer be bypassed.** The tool wrapped
  the validated SQL as `SELECT * FROM (<sql>) AS _q LIMIT :n`, so a query
  ending in `) AS x --` closed the subquery and commented out the `LIMIT`,
  returning (and loading into memory) the whole table. The SQL now runs
  unwrapped and the cap is applied with `fetchmany(limit)` on a server-side
  cursor. Internal `;` is rejected, so only one statement can run. Still
  open: the `api_keys` regex guard can be bypassed with dynamic SQL such as
  `query_to_xml`; the fix is a read-only database role (see
  `docs/audits/ai-review-verification-oct31.md`, C-1/M-2).
- **CORS is off by default, in code and in the env templates.** Phase 5
  changed the code default from `["*"]`, but `.env.example` and
  `.env.security.example` still set `["*"]`, so a fresh `cp .env.example .env`
  re-enabled it. The default is now `[]` everywhere (no Grimoire client needs
  CORS), and `auth.cors_origins` is validated: entries must be
  `scheme://host[:port]` with no path, since a trailing slash silently never
  matches, and `"*"` must be the only entry, since Starlette otherwise allows
  every origin. `["*"]` on its own still works but logs a warning.
  **Breaking** only if you serve a browser app from another origin; list it
  explicitly.
- **Nothing is published beyond loopback except the TLS proxy.** On a VPS,
  Docker's published ports bypass UFW, so `api` (8001), `mcp` (8100),
  `pgadmin` (5050, which has no login in desktop mode) and `redis-commander`
  (8081) were reachable from the internet. All four now bind to `127.0.0.1`,
  like the datastores. **Breaking:** remote clients must go through the new
  Caddy proxy or an SSH tunnel. `tests/deploy/test_network_exposure.py` fails
  if any non-proxy service publishes on all interfaces.
- **Caddy TLS reverse proxy** (compose profile `proxy`, `deploy/caddy/Caddyfile`).
  Automatic Let's Encrypt certificates for `GRIMOIRE_DOMAIN`, `/mcp/*` routed to
  the `mcp` service with unbuffered SSE, everything else to `api`, plus HSTS and
  a 110 MB body cap. `FORWARDED_ALLOW_IPS` (default `172.16.0.0/12`, override
  with `GRIMOIRE_TRUSTED_PROXIES`) makes uvicorn trust Caddy's
  `X-Forwarded-For`, so rate limits see real client IPs.
- **Removed `CHROMA_SERVER_AUTHN_TOKEN`.** Chroma 1.x dropped built-in server
  auth, so the token was silently ignored and only gave a false sense of
  security. ChromaDB access control is network isolation; see
  `docs/deploy/docker.md#chromadb-access-control`.
- **Server paths are confined everywhere.** Only REST file/directory ingest
  checked the path; REST `watch/start` and the MCP tools `grimoire_watch_start`,
  `grimoire_ingest_file` and `grimoire_ingest_directory` accepted any path, so a
  `dvl` key could make the server read and index anything it could open. All
  five now share one guard (`grimoire.utils.path_guard`) that resolves `..` and
  symlinks and requires the path to sit inside `api.allowed_roots`. **Breaking:**
  the default is `["/tmp"]`; widen it with
  `GRIMOIRE_API__ALLOWED_ROOTS='["/data/watch","/tmp"]'`. Refusals say which
  roots are allowed but never echo the rejected path. Cloud/rclone watches are
  not path-checked (their paths are remote names), and the `grimoire` CLI is
  unrestricted since running it needs a shell on the host. Also: an unknown
  watch backend or an already-watched path is now a 400, not a 500.

### Added

- **`grimoire.vectorstore.factory`**: `create_vector_store()` and
  `get_maintenance_store()` replace seven hand-copied `ChromaDBStore(...)`
  blocks. Maintenance callers (delete, `status`, `/readyz`) open the store with
  the new `ChromaDBStore.connect()` instead of a fake `embedding_dim=1`, and
  reuse one client instead of creating one per request. Setting
  `vector_store.type: qdrant` now fails with a clear error instead of silently
  using ChromaDB.
- **CI on every pull request.** Three gating workflows run on each PR: lint and
  type check (the `pre-commit` gate), unit tests on Python 3.12 and 3.13, and
  integration tests that run `tests/integration/test_live_api.py` against a
  docker compose stack built from the PR, not the deployed server. A fourth,
  advisory workflow posts a code review from an Ollama model
  (`scripts/ci/ollama_review.py`) as a sticky PR comment. It runs on the
  runner's CPU by default, or on your own server via the `OLLAMA_HOST` secret.
  See "Continuous integration" in the README.

- **Watcher control in the TUI.** A fifth tab (`F5`) lists the directories the
  server is watching with its files-processed and failed counters; `n` starts a
  watch (a path on the API's machine, recursive by default) and `x` stops the
  highlighted one. Start and stop need a `dvl` or `agt` key; the server's
  refusal (for example a path outside `api.allowed_roots`) is shown in the
  dialog. The shared client gains `watch_status()`, `start_watch()` and
  `stop_watch()`, and a deliberate 503 (server started without `--watch`) now
  surfaces its reason instead of a generic server error.

- **Content generation in the TUI.** `g` in a document's detail view opens a
  Generate screen: summary, flash cards, cliff notes, outline, or an extract
  (with a question). The result is shown in place with the model, duration and
  whether it was cached; generated text is displayed as plain text and capped
  for display. Needs a `dvl` or `agt` key. The shared client gains `generate()`,
  which validates what the server would refuse and waits for the long timeout.

- **Ingest in the TUI.** A fourth tab (`F4`): type the path of a file on the
  machine running the TUI and press Enter to upload it. Uploads run one at a
  time with a result row each (done, skipped as a duplicate, or failed with the
  reason), and a failure does not stop the rest. Auto-tag can be switched off.
  Single files only. After a successful ingest the Documents and Categories
  tabs reload when next shown. Needs a `dvl` or `agt` key.

- **Categories and tags in the TUI.** A third tab (`F3`) lists categories with
  how many documents carry each, and `n` creates one (name, description,
  optional parent). In a document's detail view, `a` adds a tag from the
  categories it lacks and `x` removes one of its own; the Documents table
  refreshes afterwards. All of this needs a `dvl` or `agt` key; a read key is
  refused by the server and the reason is shown.
- `PUT` / `DELETE /api/v1/documents/{id}/tags/{category_id}` (dev tier,
  idempotent) to assign or remove a tag. Until now tags were only ever set by
  auto-tagging at ingest.
- `GET /api/v1/categories` returns `document_count` per category, and
  `GET /api/v1/documents/{id}` returns `categories` (with ids) alongside the
  existing name-only `tags`. Both are additive.
- The shared client gains `list_categories`, `create_category`, `tag_document`
  and `untag_document`.

### Security

- **BREAKING: the REST API now enforces API key tiers.** Previously tiers only
  changed the rate limit over REST, so a read-tier key (`grim_rdl_...`) could
  delete documents, ingest files, generate content and start watchers; only the
  MCP server checked tiers. REST now matches MCP (agent includes dev includes
  read):
  - `rdl` and above: every `GET`, `POST /query/ask`, `POST /query/search`.
  - `dvl` and above: `POST /ingest/file|directory|upload`, `POST /generate`,
    `POST /categories`, `POST /watch/start`, `DELETE /watch/{id}`.
  - `agt` only: `DELETE /documents/{id}`, `DELETE /categories/{id}`.

  A key below the required tier gets `403` naming the tier needed. If a script
  uses a read key to write, issue it a `dvl` or `agt` key with
  `grimoire key create --tier dev|agent`. A test fails if a new write route is
  added without a tier.

### Added

- **Document search.** `GET /api/v1/documents` takes a `q` parameter: a
  case-insensitive substring match on the title or source path, composable with
  the existing filters and pagination (`%` and `_` in the text match literally).
  The shared client's `list_documents` accepts `q`, and the TUI's Documents pane
  has a search box (`/` to focus, `Enter` to apply, `Esc` to clear).

### Fixed

- **Tracing exports spans.** `observability.tracing: true` instrumented
  FastAPI and SQLAlchemy but never installed a tracer provider, so
  OpenTelemetry's no-op default discarded every span. `grimoire.utils.tracing`
  now installs one with an OTLP/gRPC exporter configured from
  `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_SERVICE_NAME`, and instruments Redis
  (the package was a dependency but unused).
- **Cached answers are invalidated on every document change.** Only the REST
  delete route and the watcher invalidated, through a `hasattr` guard that hid
  wiring mistakes. REST and MCP ingest, tagging, category deletes and the CLI
  never did, so `ask`/`search` could serve pre-change answers for up to the
  result TTL (1 day by default). All of them now call
  `grimoire.core.cache.invalidate_query_results()` after their commit. It
  clears only the query namespace and works across processes, since the cache
  is a shared Redis server or a shared cache directory.
- **Query-cache invalidation no longer wipes embeddings and generated
  content.** The query agent shared its cache with the embedder (and, on disk,
  the same directory as content generation), and `invalidate_cache()` runs
  after every watcher ingest and every delete. Each one cleared every cached
  embedding and every generated summary, flashcard and outline (30-day TTL).
  `DiskCache` now takes a `namespace`: keys are prefixed and entries tagged
  with it, and `clear()` evicts only that namespace. Entries cached before
  this change are untagged and simply age out.
- **The Redis cache backend works.** With `cache.storage: redis`, every agent
  builder crashed: they called `CacheFactory.create(backend="redis", path=...)`
  and `RedisCache` takes no `path`. The Redis host, port, password and
  `redis.db_cache` were never passed either. Caches are now built with
  `CacheFactory.from_settings()`, and embeddings, query results and generated
  content each get their own namespace.
- **`grimoire reindex` can repair missing embeddings again.** It opened the
  vector store with a placeholder `embedding_dim=1`, so every write failed the
  store's own dimension check ("dimension 768, expected 1") and nothing was
  re-embedded. It now opens the store with the embedder's real dimension.
- **Watcher: renames, atomic saves and delete races.**
  - A renamed file (`a.pdf` → `b.pdf`) now removes the old path's document and
    ingests the new path. MOVED events used to be ignored entirely.
  - Edits from editors that save atomically (write a temp file, rename it over
    the original) are now re-ingested; they also arrived as MOVED.
  - DELETED events use the same extension filter as creates, so deleting a
    `.log` or swap file no longer triggers a document lookup.
  - If a deleted local file is back on disk before the delete is processed
    (delete-and-recreate saves), its document is kept.
  - A crashed event processor is restarted by the next `watch()`, and events
    arriving after `stop_all()` are dropped instead of touching the asyncio
    queue from the observer thread.
- **`grimoire cache stats` and `grimoire status --detailed` no longer crash on
  the disk cache.** `DiskCache.get_stats()` became async when its sqlite I/O
  moved off the event loop, but both commands still called it synchronously
  and failed with `'coroutine' object has no attribute 'get'`.
- **A Redis outage no longer takes the API down.** When Redis was unreachable,
  slowapi passed the connection error to its 429 handler, which crashed reading
  `.detail`, so every endpoint except `/health` returned 500. The limiter now
  falls back to in-memory counters with the same limits (per process) and
  re-checks Redis with exponential backoff. Limits stay enforced during the
  outage rather than failing open.
- **Malformed IDs in REST paths are 404s, not 500s.** `GET`/`DELETE
  /api/v1/documents/{id}`, `DELETE /api/v1/categories/{id}` and the
  `/documents/{id}/tags/{category_id}` routes passed the raw path segment to
  Postgres, which rejects anything that is not a UUID, so a typo produced an
  internal server error. They now answer "not found" without querying. Found by
  the new compose-stack integration tests; unit tests mock the database and
  could not see it.

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
