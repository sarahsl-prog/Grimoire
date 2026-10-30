# Codebase Review Response — Grimoire

**Audited file**: `docs/audits/codebase-review-oct18.md`
**Validation date**: 2026-10-18
**Scope**: Every issue in the audit was re-checked against the current source. No code changes were made.

**Validation summary**: All substantive findings in the audit were confirmed correct. A few minor numeric or emphasis caveats are noted inline (e.g., the remaining API-key secret entropy after the 12-character prefix is 40 characters, not ~43). The "Verified fine" positive findings were not re-challenged because they describe controls that do exist; this response lists only the issues that require action.

---

## How to read this response

- **Status**: `correct` = issue is present and described accurately; `partial` = core concern is present but a detail in the audit is slightly off.
- **Recommended resolution**: the smallest fix that closes the issue, not the most ambitious refactor.
- **Estimated fix time**: engineering-only wall-clock estimate for someone familiar with the codebase. Testing and review time is included for critical/major items.

---

## Critical

### C-1: Document deletion never removes vectors

| | |
|---|---|
| **Status** | correct |
| **Location** | `grimoire/api/routes/documents.py:343-366`, `grimoire/mcp/tools.py:1099-1112` |
| **Current behavior** | Both delete paths import `grimoire.services.vector_store.get_vector_store_service`, but `grimoire/services/` does **not** exist. The resulting `ImportError` is swallowed at `debug` level, so vector cleanup is dead code. The watcher also ignores `DELETED` events (`grimoire/agents/watcher.py:312-317`), and hybrid search serves ChromaDB results without filtering against Postgres. Deleted documents therefore remain searchable forever. |
| **Recommended resolution** | 1. Implement vector deletion by calling the existing `ChromaDBStore` directly (or create the `grimoire.services.vector_store` module if the intent was an abstraction). 2. Remove the broad `except Exception`/`except ImportError` silence around cleanup so failures are logged as errors and returned to the caller. 3. Add an integration test that deletes a document and asserts the chunk count in ChromaDB is zero. 4. Decide whether the watcher should handle `DELETED` events (see design decisions). |
| **Estimated fix time** | 1–2 days |

---

## Major

### F-1/F-2/F-3: Docker Compose publishes unauthenticated datastores

| | |
|---|---|
| **Status** | correct |
| **Location** | `docker-compose.yml:49-112`; `docker-compose.security.yml:26-50`; `docs/deploy/hetzner_security.md` |
| **Current behavior** | Postgres binds to `0.0.0.0:${POSTGRES_PORT:-5434}:5432` with a fallback password `grimoire`. Redis binds to `0.0.0.0:${REDIS_PORT:-6379}:6379` with no `--requirepass`. ChromaDB (`chromadb/chroma:latest`) binds to `0.0.0.0:${CHROMADB_PORT:-8060}:8000` with no auth token. The security overlay targets a public Hetzner VM and adds additional published ports for Postgres and Chroma; the only mitigation is a documented optional `ufw` step. |
| **Recommended resolution** | 1. Bind all infrastructure services to loopback: `"127.0.0.1:${POSTGRES_PORT:-5434}:5432"`, `"127.0.0.1:${REDIS_PORT:-6379}:6379"`, `"127.0.0.1:${CHROMADB_PORT:-8060}:8000"`. 2. Remove the Postgres password fallback (`${POSTGRES_PASSWORD}` without `:-grimoire`). 3. Add `redis-server --requirepass ${REDIS_PASSWORD}` and wire `GRIMOIRE_REDIS__PASSWORD` into compose. 4. Set `CHROMA_SERVER_AUTHN_TOKEN` in the Chroma environment. 5. In the security overlay, drop infrastructure `ports:` entirely so only the API/MCP are reachable. 6. Replace `chromadb/chroma:latest` with a pinned version tag or digest. |
| **Estimated fix time** | 4–8 hours |

### F-4/F-7: MCP `pg_query` is broken and allows unrestricted reads at DEV tier

| | |
|---|---|
| **Status** | correct |
| **Location** | `grimoire/mcp/tools.py:1117-1144` (tool), `:362-374` (validator), `grimoire/db/models.py:1165-1215` (`api_keys`) |
| **Current behavior** | The tool wraps user SQL as a bind parameter in a FROM clause (`FROM (:inner_query) AS _grimoire_q`), which is invalid Postgres syntax, so every call fails. The validator only checks `SELECT`/`WITH` shape and rejects `SELECT INTO`; a data-modifying CTE passes it. The tool runs against the same DB manager as the rest of the app, so a DEV key can `SELECT` from `api_keys` and other tables; there is no read-only role or statement timeout. Raw exceptions are returned to the client. |
| **Recommended resolution** | 1. Rebuild query execution to use a read-only Postgres role (or `SET TRANSACTION READ ONLY` + `SET LOCAL statement_timeout`). 2. Interpolate the verified subquery only after regex-rejecting data-modifying keywords (`DELETE|UPDATE|INSERT|MERGE|ALTER|CREATE|GRANT|COPY|DROP`). 3. Return a generic error to the client and log the full exception server-side. 4. Restrict the role to read tables only, not `api_keys`. |
| **Estimated fix time** | 1–2 days |

### F-5: Tier-specific rate limits are dead configuration

| | |
|---|---|
| **Status** | correct |
| **Location** | `grimoire/api/rate_limit.py:52-54`, `grimoire/api/rate_limit.py:87-91`, `grimoire/config/settings.py:799-806` |
| **Current behavior** | `get_tier_rate_limit`, `DEFAULT_TIER_RATE_LIMITS`, and `settings.auth.rate_limits` exist but are never used. No route applies `@limiter.limit(...)` for tier-specific limits; only the flat `30/minute` default applies to all tiers. |
| **Recommended resolution** | Option A: implement dynamic tier limits in `create_app` using a slowapi callable that reads `request.state.api_key.tier`. Option B: remove the dead config, fields, and documentation references if the current flat 30/min is acceptable. Pick one and update the docs. |
| **Estimated fix time** | 2–4 hours (implement) / 1 hour (delete) |

### M-1: Query result cache key omits `top_k`/model and is never invalidated

| | |
|---|---|
| **Status** | correct |
| **Location** | `grimoire/agents/query.py:471-475, 495, 522, 526`; REST delete (`api/routes/documents.py:328-379`); MCP delete (`mcp/tools.py:1071-1114`) |
| **Current behavior** | The cache key is `json.dumps({"query": ..., "filters": ...})`. `top_k`, LLM model, temperature, etc. are omitted, so a `top_k=100` request can reuse a `top_k=5` result. The hard-coded TTL is 3600 seconds. Delete and re-ingest paths never invalidate the cache, so stale answers citing dead chunks can be served for up to an hour. |
| **Recommended resolution** | 1. Include every parameter that affects the result in the cache key: normalized query, filters, `top_k`, `max_context_chunks`, LLM model, temperature. 2. Invalidate query-cache entries when a document is deleted or re-ingested (publish an event or call a cache-invalidating helper from the delete/re-ingest paths). 3. Add tests covering `top_k` key variance and delete invalidation. |
| **Estimated fix time** | 1–2 days |

### M-2/M-3: Watcher thread→event-loop handoff is not thread-safe

| | |
|---|---|
| **Status** | correct |
| **Location** | `grimoire/agents/watcher.py:287-301` (callback), `grimoire/storage/watch_manager.py:356-399` (observer thread) |
| **Current behavior** | The watchdog observer thread calls `asyncio.Queue.put_nowait(...)` and `asyncio.create_task(...)` directly. Both are not thread-safe. When the event-loop task is parked in `await queue.get()`, `future.set_result` from the wrong thread raises `RuntimeError` inside the observer thread and is swallowed, so file-change events are silently dropped. Async callbacks never fire for local watches because there is no running loop in the observer thread. |
| **Recommended resolution** | 1. Store the event loop on `WatchManager` initialization. 2. In the observer callback, use `loop.call_soon_threadsafe(queue.put_nowait, (watch_id, change))`. 3. For async callbacks, schedule them with `loop.call_soon_threadsafe(asyncio.create_task, self._async_callback_wrapper(change))`. 4. Add a test that dispatches a synthetic watchdog event from a background thread and asserts it reaches the async processor. |
| **Estimated fix time** | 4–8 hours |

### M-4: Synchronous embedding inference blocks the async event loop

| | |
|---|---|
| **Status** | correct |
| **Location** | `grimoire/core/embedder.py:256-263` and `:338-358` |
| **Current behavior** | `embed()` and `embed_single()` are `async def` but call `model.encode(...)` directly. On first call this downloads and loads the sentence-transformers model, which can take tens of seconds, freezing all concurrent requests including `/health`. The Docling parser already uses `run_in_executor`; the embedder does not. |
| **Recommended resolution** | 1. Wrap `model.encode` in `asyncio.to_thread()` (or `loop.run_in_executor`) and use a small pool/queue to bound concurrent inference. 2. Consider pre-loading the model during startup lifespan rather than on first request, with a startup probe that fails fast if the model is unavailable. 3. Add a timeout to the embed operation. |
| **Estimated fix time** | 1–2 days |

### M-5: ChromaDB operations block the event loop; `PersistentClient` is shared across processes

| | |
|---|---|
| **Status** | correct |
| **Location** | `grimoire/vectorstore/chromadb.py:232, 333, 493, 526, 595, 625`; `cli/helpers.py:99-104, 138-143` |
| **Current behavior** | All ChromaDB calls are synchronous and run inline in async methods with no executor. The embedded `PersistentClient` is single-process, yet the API server, `grimoire watch start`, and `grimoire ingest` each construct their own client on the same `chroma_db/` path, leading to `database is locked` errors and possible index corruption. |
| **Recommended resolution** | 1. Wrap ChromaDB calls in `asyncio.to_thread()`. 2. Move production deployments to the server-mode `chromadb.HttpClient` (single writer) or add an internal queue/lock so only one process writes vectors. 3. Document that running multiple CLI ingest/watch processes against the same `PersistentClient` path is unsupported until server mode is adopted. |
| **Estimated fix time** | 2–4 days |

### M-6: Pagination parameters on the documents list endpoint are unbounded

| | |
|---|---|
| **Status** | correct |
| **Location** | `grimoire/api/routes/documents.py:74-75, 143` |
| **Current behavior** | `offset: int = 0` and `limit: int = 50` accept any signed integer. Negative `offset` causes a Postgres syntax error and a 500 response with a raw DB message; very large `limit` can OOM the server by loading the full table into memory. |
| **Recommended resolution** | Add FastAPI `Query(ge=0)` constraints and cap `limit` (e.g., `le=250` or `le=1000`). Return 422 for out-of-range inputs. |
| **Estimated fix time** | 1–2 hours |

### M-7: REST `/watch` endpoints permanently return 503

| | |
|---|---|
| **Status** | correct |
| **Location** | `grimoire/api/routes/watch.py:27-42`; server entrypoints in `grimoire/api/main.py` and `grimoire/cli/main.py` |
| **Current behavior** | `_get_watcher()` returns 503 when module-global `_watcher` is `None`. `set_watcher()` is defined but has zero callers; there is no `--watch` flag or lifespan registration. Every `POST /api/v1/watch/start`, `DELETE /api/v1/watch/{id}`, and `GET /api/v1/watch/status` always returns 503, despite the TUI/README documenting these endpoints. The MCP server wires its own watcher and works. |
| **Recommended resolution** | Either: (A) wire `set_watcher()` during the API lifespan, with a durable watch-state store so watches survive restart; or (B) remove the REST watch routes and update the TUI/README to use MCP/CLI watch only. See design decisions. |
| **Estimated fix time** | 1–2 days (wire + state store) / 2–4 hours (remove) |

### M-8: CLI `grimoire watch unwatch` cannot stop an active watch

| | |
|---|---|
| **Status** | correct |
| **Location** | `grimoire/cli/watch.py:134-147` |
| **Current behavior** | `unwatch` constructs a brand-new `WatchManager()` and `WatcherAgent` in a short-lived CLI process. Active watches live in the long-running `grimoire watch start` process, so `stop_watch` never finds the `watch_id`. |
| **Recommended resolution** | Persist watches to Postgres and have `unwatch` write a stop flag that the running `watch start` process polls, or migrate watch management to the API process and delete the standalone `watch start` daemon. |
| **Estimated fix time** | 1–2 days |

### No CI/CD pipeline

| | |
|---|---|
| **Status** | correct |
| **Location** | Repository root |
| **Current behavior** | No `.github/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/`, `azure-pipelines.yml`, `Makefile`, or other CI exists. Quality gates (ruff, black, mypy, bandit, pytest) run only through local pre-commit. There is no dependency/image security scanning. |
| **Recommended resolution** | Add a GitHub Actions workflow that runs `uv sync`, `pytest -m unit`, `pre-commit run --all-files`, and a container image scan (trivy/grype). `tests/deploy/` is already CI-friendly text parsing. |
| **Estimated fix time** | 4–8 hours |

### Celery, OTEL/tracing, and structured JSON logging are phantom config surfaces

| | |
|---|---|
| **Status** | correct |
| **Location** | `pyproject.toml:41`, `.env.example:70-71,76-82,145-149`, `grimoire/config/settings.py:339-340,360-394,600-601`, `grimoire/utils/logger.py:30-88` |
| **Current behavior** | Celery is a hard runtime dependency with compose env vars and a validated `CeleryConfig`, but there are zero `import celery` in `grimoire/` and no worker service. OTEL/tracing/metrics env vars and fields exist but no `opentelemetry` dependency or code. `LOGGING__STRUCTURED`/`USE_JSON` are validated but never read by `setup_logger()`. |
| **Recommended resolution** | Implement the advertised behavior or delete the surface. Suggested order: 1. Decide whether Celery is actually needed (likely not for current architecture) and remove it. 2. Add JSON loguru sink controlled by `use_json` (~5 lines in `utils/logger.py`). 3. Remove OTEL fields/env vars until OpenTelemetry is actually adopted. |
| **Estimated fix time** | 1–2 days (all three) |

### Duplicate `dev` dependency definitions with conflicting version floors

| | |
|---|---|
| **Status** | correct |
| **Location** | `pyproject.toml:69-88` vs `:317-323` |
| **Current behavior** | Both `[project.optional-dependencies].dev` and `[dependency-groups].dev` exist. They conflict on pytest-asyncio (`>=0.23` vs `>=1.3.0`), pytest-cov (`>=4.1` vs `>=7.1.0`), pytest-httpx (`>=0.30` vs `>=0.36.2`), and ruff (`>=0.3` vs `>=0.15.8`). The two lists also include different tool sets. |
| **Recommended resolution** | Consolidate on `[dependency-groups].dev` (the uv-native mechanism), align version floors, and either remove the `dev` extra or make it a thin reference to the group. Update docs (`docs/UPGRADING.md`) that mention `uv sync --extra dev`. |
| **Estimated fix time** | 2–4 hours |

---

## Minor / Polish

| ID | Status | Location | Issue | Recommended resolution | Estimated fix time |
|---|---|---|---|---|---|
| F-6 | correct | `grimoire/mcp/tools.py:416-423` | JSONB fallback ILIKE pattern does not escape caller-supplied `%`/`_`, causing over-matching on SQLite/dev. | Escape `%`, `_`, and `"` like `documents.py:_escape_like` before building the pattern. | 1–2 hours |
| F-8 | correct | `grimoire/config/settings.py:807-810`, `api/main.py:152-158` | CORS defaults to `allow_origins=["*"]`. Risk is low (no credentials, header auth), but default-open. | Default to `["http://localhost:8001"]` and require explicit opt-in for broader origins. | 1 hour |
| F-9 | correct | `grimoire/config/settings.py:742-791` | `api.secret_key` is validated as production-required but never consumed. | Either implement JWT/signed URLs or remove the field and update `.env.example`. | 1–2 hours |
| F-10 | correct | `old.env.bak` (repo root) | Plaintext backup of a real environment (contains `POSTGRES_PASSWORD` and an `OLLAMA_API_KEY`). Gitignored but present on disk. | Delete the file; optionally add `*.bak` scanning to pre-commit or CI. | 5 minutes |
| F-11 | correct | `grimoire/mcp/app.py:63-65` vs `api/main.py:196-208` | Standalone MCP `/health` has no rate limiter while the API `/health` has a 60/min in-memory limiter. | Reuse `create_health_limiter()` in `create_mcp_app`. | 1–2 hours |
| F-12 | correct | `grimoire/api/schemas.py:104-107`, `mcp/tools.py:92-94,108-110` | REST query inputs have no `max_length`; MCP caps them at 2000/4000 chars. | Add `min_length=1, max_length=4000` to `QueryRequest.query` and `SearchRequest.query`. | 1 hour |
| F-13 | correct | `alembic.ini:8` | `sqlalchemy.url = postgresql+asyncpg://grimoire:grimoire@localhost:5432/grimoire` in a committed file (inert because `alembic/env.py` overrides). | Replace with the Alembic placeholder convention `driver://user:pass@localhost/dbname` plus a comment pointing at `env.py`. | 5 minutes |
| Secret placeholder bypass | correct | `grimoire/config/settings.py:779-791`, `.env.example:159` | Validator rejects only the exact string `"change-me-in-production"`; the example value `your-secret-key-change-me-in-production` passes. | Extend the validator to reject any value containing `change-me` case-insensitively, and leave the example value empty/commented. | 1 hour |
| Q-1 | partial | `grimoire/api/auth.py:46-49,61-63,77-94` | Prefix lookup creates a timing oracle for key *existence*. Audit says remaining ~43 chars stay secret; actual remaining entropy after the 12-char prefix is 40 chars (format `grim_<tier>_<3-random-chars>`). No key material is exposed. | Optional: add a Redis/cache lookup layer (already noted as a TODO in `auth.py`) to flatten timing. Acceptable to document as a known tradeoff. | 2–4 hours if implemented |
| Q-3 | correct | `grimoire/mcp/auth_stdio.py:23`, `mcp/router.py:118-120` | MCP tier context is ContextVar-based and safe per-request, but the safety is implicit. | Add a comment explaining why per-task asyncio isolation keeps it safe, plus a test that two concurrent SSE requests see different tier values. | 1–2 hours |
| m-1 | correct | `grimoire/core/chunker/recursive.py:153-205` | Configured `chunk_overlap` is dead code; `_merge_splits_with_overlap` has zero callers. | Remove the dead code and the misleading docstring, or wire overlap into `_recursive_split()`. | 2–4 hours |
| m-2 | correct | `grimoire/vectorstore/chromadb.py:384-389` | `$contains` is rewritten to `$in: [val]` (exact single-value match, not membership/substring). | Rename the operator to match actual behavior, or implement true membership check and document the limitation. | 1–2 hours |
| m-3 | correct | `grimoire/vectorstore/chromadb.py:461-464,562-564` | ChromaDB metadata read path splits any comma-containing string into a list. | Stop treating scalar strings containing commas as CSV lists; only split values that were originally stored as lists. | 2–4 hours |
| m-5 | correct | `grimoire/storage/local.py:499-507` | `LocalStorageAdapter.watch()` logs "Started watching" but never calls `observer.start()`; `_observers` grows unboundedly. | Either start the observer inside `watch()` or remove this method if production uses `WatchManager` exclusively; document which path is canonical. | 1–2 hours |
| m-6/m-7 | correct | `storage/watch_manager.py:385-388`, `storage/local.py:181-190`, `agents/watcher.py:312-317` | MOVED event `path`/`previous_path` semantics are inverted between the two handlers; DELETED and MOVED events are dropped by the watcher. | Decide the intended behavior (see design decisions). If MOVED/DELETED are meant to be supported, fix event mapping and add deletion handling. | 1–2 days if implemented |
| m-8 | correct | `grimoire/core/cache.py:605-687` | `DiskCache` does sync SQLite I/O inside async methods, `clear()` wipes the entire shared cache directory (all namespaces), and `get()` swallows all errors while `set()` raises. | Move sync diskcache calls to `to_thread()`, scope `clear()` to the configured namespace prefix, and make error handling symmetric (log + raise, or log + degrade consistently). | 1–2 days |
| m-9 | correct | `grimoire/db/session.py:114-123` | `initialize_db()` overwrites the global manager without disposing the previous engine, leaking asyncpg pools on re-invocation. | Call `_db_manager.engine.dispose()` before replacement, or make the manager a singleton that refuses re-initialization unless explicitly reset. | 1–2 hours |
| m-10 | correct | `grimoire/cli/docs.py:40-45,121-123`, `cli/keys.py:40-42` | `"m"` means 30-day months in `docs.py` but minutes in `keys.py`; ISO dates parse naive and are compared against an aware DB column. | Standardize unit suffixes (e.g., `mo` for months, `m` for minutes) and parse ISO dates with `datetime.fromisoformat(...).replace(tzinfo=UTC)` when no offset is given. | 2–4 hours |
| m-11 | correct | `grimoire/agents/ingestion.py:727-732,844-884` | Old vectors are deleted before DB commit; new vectors are upserted before DB commit. A DB rollback leaves the vector store inconsistent. | Implement a two-phase or compensating-transaction pattern: stage vector changes, commit DB, then finalize vectors; on rollback, revert staged vector changes. | 2–4 days |
| m-12 | correct | `app.py`, `rag_pipeline.py` (repo root) | Drifted Streamlit/FAISS prototype using `FAISS.load_local(..., allow_dangerous_deserialization=True)`, bypassing all package controls. | Delete the files or move them to `examples/` with a prominent warning that they are unsupported. | 30 minutes |
| m-13 | correct | `grimoire/api/rate_limit.py:77-79` | Redis rate-limit URL inserts the password raw: `f"redis://:{settings.redis.password}@..."`. | Build the URL with `urllib.parse.quote` or use redis client kwargs instead of a URL string. | 1 hour |
| m-14 | correct | `grimoire/mcp/server.py:57-64`, `mcp/tools.py:75-79` | MCP lifespan `finally` calls `tools._get_mcp_watcher()`, which lazily constructs the full watcher just to stop nothing. | Check the global sentinel before building; only call `stop_all()` if a watcher was actually created. | 1 hour |
| No `/readyz` | correct | `grimoire/api/main.py:200-203`, `mcp/app.py:63-65` | Only shallow `/health` exists; no readiness probe checks Postgres/Chroma/Redis. | Add an authenticated `/readyz` that pings DB and Chroma; keep `/health` shallow. | 2–4 hours |
| `GRIMOIRE_API__WORKERS` ignored | correct | `.env.example:158`, `docker-compose.yml:157-158,180-181` | Compose runs bare uvicorn with no `--workers` flag. | Either pass `--workers ${GRIMOIRE_API__WORKERS:-1}` or remove the env var/docs claim if single-worker is intentional for async I/O-bound workloads. | 1 hour |
| Watcher service no healthcheck | correct | `docker-compose.yml:200-218` | The `watcher` service has `restart: unless-stopped` but no `healthcheck`. | Add a lightweight healthcheck (e.g. touch-file timestamp or process check). | 1–2 hours |
| Untested infrastructure | correct | `grimoire/utils/token_crypto.py`, `grimoire/utils/observability.py` | No corresponding test files exist. | Add unit tests for Fernet token round-trip, key file permissions, and the observability decorator stack. | 2–4 hours |

---

## Design decisions required

Before several fixes can land, the project needs explicit decisions on architecture and scope. These should be documented in `docs/DESIGN.md` or as ADRs once resolved.

1. **Document deletion lifecycle**
   - Should deleting a document also delete its generated wiki pages, cached query results, and derived content (summaries, flashcards)?
   - Should the watcher handle `DELETED` events to automatically remove files that disappear from disk, or is deletion strictly an explicit API/CLI operation?
   - Should hybrid search filter out orphaned ChromaDB chunks whose `document_id` no longer exists in Postgres, as a defense-in-depth safety net?

2. **Watch service architecture**
   - Is the REST `/watch` surface intended to be functional? If yes, the watcher should be instantiated in the API lifespan and watch state should be persisted to Postgres so it survives restarts. If no, the routes should be removed and the TUI/README updated to use CLI `watch start` / MCP only.
   - Should `grimoire watch start` remain a standalone long-running process, or should all watch management move into the API/watcher container with a durable state store?

3. **Vector-store access model**
   - For multi-process deployments, should Grimoire standardize on ChromaDB server mode (`HttpClient`) with a single container, or keep `PersistentClient` and serialize all vector writes through a dedicated worker/queue?
   - Should the embedder become a separate microservice, or stay in-process wrapped in `to_thread`?

4. **Celery / task-queue future**
   - Is Celery intended for future background ingestion/generation? If yes, implement a worker service and wire tasks. If no, remove the dependency, compose env vars, settings model, and docs.

5. **Observability strategy**
   - Are OpenTelemetry tracing/metrics going to be implemented? If yes, add `opentelemetry` dependencies and instrumentation; if no, remove the env vars and settings.
   - Should `LOGGING__USE_JSON` be implemented as a JSON loguru sink, or removed?

6. **Security-filter model unification**
   - The API stores security facets in dedicated indexed columns (`severity`, `cve_id`, `mitre_technique_id`) while MCP tools query the same concepts via JSONB blob operators. Which model is canonical? Should the JSONB fallback be removed once all security sources populate the indexed columns?

7. **Event semantics for moved/deleted files**
   - Which handler is authoritative for MOVED event field mapping (`path` vs `previous_path`)?
   - Should MOVED events trigger re-indexing, a rename update, or be ignored?

---

## Recommended fix order

1. **Critical**: C-1 (vector deletion never works).
2. **Major security**: F-1/F-2/F-3 (Compose datastore exposure), F-4/F-7 (MCP `pg_query`).
3. **Major operational**: Add CI, remove or implement phantom Celery/OTEL/JSON config, reconcile duplicate `dev` deps.
4. **Major correctness**: M-2/M-3 (watcher thread safety), M-4 (embedder blocks loop), M-5 (ChromaDB multi-process), M-1 (cache key/invalidation), M-7/M-8 (watch surface).
5. **Minor/polish**: pagination bounds, CORS default, secret placeholder, FAISS prototype removal, metadata comma mangle, ILIKE escaping, Redis URL escaping, `old.env.bak` deletion, tests for token_crypto/observability.

---

*All listed issues were validated against the current codebase by re-reading the cited files. No code changes were made in the process.*