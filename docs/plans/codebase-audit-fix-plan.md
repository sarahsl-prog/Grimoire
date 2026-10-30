# Grimoire Codebase Audit — Implementation Plan

**Source audit**: `docs/audits/codebase-review-oct18.md`  
**Validation response**: `codebase-review-response.md`  
**Branch**: `audit-updates`  
**Plan date**: 2026-10-30

---

## Goal

Resolve every confirmed finding from the October 2026 codebase review, grouped by risk and dependency order. Each phase is designed to be completable in 1–3 days of focused work and produces a merge-ready set of changes.

---

## Phase 0 — Prerequisites (0.5 day)

Before any fixes land, resolve the items that make the repo safer to work in and remove obvious debris.

| Task | ID | Acceptance Criteria |
|------|-----|---------------------|
| Delete `old.env.bak` from repo root | F-10 | File removed; `*.bak` added to `.gitignore` if not already present |
| Remove or relocate drifted prototypes | m-12 | `app.py` and `rag_pipeline.py` moved to `examples/` with a `README.md` warning, or deleted entirely |
| Pin ChromaDB image in compose | F-1/F-2/F-3 | `chromadb/chroma:latest` replaced with a digest or specific version tag in `docker-compose.yml` |

---

## Phase 1 — Critical (1–2 days)

**Theme**: Data integrity — deleted documents must actually disappear.

| Task | ID | Details | Acceptance Criteria |
|------|-----|---------|---------------------|
| Implement vector deletion | C-1 | Fix or replace the broken `grimoire.services.vector_store` import in `documents.py:343-366` and `mcp/tools.py:1099-1112`. Call `ChromaDBStore.delete_by_document_id()` directly. Remove the broad `except Exception` / `except ImportError` silence so failures surface as errors. | Import error gone; deleting a document removes all its chunks from ChromaDB; failure raises/log is visible |
| Watcher handles `DELETED` events | C-1 (cont.) | Decide per **Design Decision #1** whether the watcher auto-removes files that vanish from disk. If yes, wire `DELETED` handling in `grimoire/agents/watcher.py:312-317`. | Events no longer silently dropped; integration test confirms deletion propagates |
| Integration test for delete→vector zero | C-1 (cont.) | Add a pytest test that ingests a document, deletes it via API, and asserts the ChromaDB collection has zero chunks for that `document_id`. | Test passes against a live (or test-scoped) ChromaDB instance |

---

## Phase 2 — Major Security (1–2 days)

**Theme**: Compose and MCP surfaces are hardened before anything touches the internet.

| Task | ID | Details | Acceptance Criteria |
|------|-----|---------|---------------------|
| Bind infrastructure services to loopback | F-1/F-2/F-3 | Change Postgres, Redis, and ChromaDB `ports:` in `docker-compose.yml` to `"127.0.0.1:${VAR:-port}:internal"`. Remove Postgres password fallback (`:-grimoire`). | `docker-compose config` shows no `0.0.0.0` bindings for datastores |
| Add Redis auth | F-1/F-2/F-3 | Pass `redis-server --requirepass ${REDIS_PASSWORD}`; wire `GRIMOIRE_REDIS__PASSWORD` into compose environment; update `.env.example` and `.env.security.example`. | Redis container starts with `--requirepass`; app connects successfully |
| Add ChromaDB auth token | F-1/F-2/F-3 | Set `CHROMA_SERVER_AUTHN_TOKEN` in Chroma environment; update `.env` templates. | Chroma rejects unauthenticated requests |
| Drop datastore ports from security overlay | F-1/F-2/F-3 | Remove Postgres/Chroma `ports:` from `docker-compose.security.yml` so only API/MCP are externally reachable. | Security compose exposes no datastore ports |
| Fix MCP `pg_query` execution | F-4/F-7 | Rebuild query execution in `mcp/tools.py:1117-1144` to use a read-only Postgres role or `SET TRANSACTION READ ONLY` + `SET LOCAL statement_timeout`. | `pg_query` returns results; data-modifying statements are rejected |
| Harden MCP query validator | F-4/F-7 | Regex-reject data-modifying keywords (`DELETE\|UPDATE\|INSERT\|MERGE\|ALTER\|CREATE\|GRANT\|COPY\|DROP`) before interpolation. | Unit tests confirm each keyword is blocked |
| Restrict read role from `api_keys` | F-4/F-7 | Ensure the read-only role cannot select from `api_keys` or other sensitive tables. | `pg_query` against `api_keys` returns permission denied |
| Generic error responses | F-4/F-7 | Return a generic client error for query failures; log full exception server-side. | Client sees only `{"error": "Query execution failed"}`; server log retains traceback |

---

## Phase 3 — Major Operational / Config Hygiene (1–2 days)

**Theme**: Remove dead code, fix dependency conflicts, and add CI before more logic changes land.

| Task | ID | Details | Acceptance Criteria |
|------|-----|---------|---------------------|
| Reconcile duplicate `dev` dependencies | Duplicate dev deps | Consolidate on `[dependency-groups].dev` (uv-native). Align version floors. Either remove the `dev` extra or make it a thin reference to the group. Update `docs/UPGRADING.md` if it mentions `--extra dev`. | `uv sync` completes with no version conflicts; `pyproject.toml` has one source of truth |
| Remove phantom Celery dependency and config | Celery phantom | Delete Celery from `pyproject.toml`, remove compose env vars, remove `CeleryConfig` from `grimoire/config/settings.py`, delete any docs references. | `grep -r celery grimoire/` returns nothing; `docker-compose.yml` has no Celery env vars |
| Remove or implement OTEL | OTEL phantom | Decide **Design Decision #5**: remove OTEL/tracing/metrics env vars and settings fields, or add `opentelemetry` deps and instrumentation. | Same grep-based acceptance as above |
| Implement or remove JSON logging | JSON logging phantom | Implement `LOGGING__USE_JSON` in `setup_logger()` (~5 lines) or delete the field/env var. | Setting toggles JSON loguru sink (if implemented) or does not exist (if removed) |
| Add CI/CD pipeline | No CI/CD | Create `.github/workflows/ci.yml` running: `uv sync`, `pytest -m unit`, `pre-commit run --all-files`, container image scan (trivy or grype). | Workflow passes on push/PR; badge in README optional |

---

## Phase 4 — Major Correctness / Performance (3–5 days)

**Theme**: Fix race conditions, event-loop blocking, and cache invalidation.

| Task | ID | Details | Acceptance Criteria |
|------|-----|---------|---------------------|
| Fix watcher thread→event-loop handoff | M-2/M-3 | Store the event loop on `WatchManager` init. Use `loop.call_soon_threadsafe(queue.put_nowait, ...)` and `loop.call_soon_threadsafe(asyncio.create_task, ...)` instead of direct calls from the observer thread. | Synthetic watchdog event dispatched from a background thread reaches async processor in <1s |
| Add watcher thread-safety test | M-2/M-3 | Test dispatches a synthetic watchdog event from a background thread and asserts the async callback fires. | Test passes consistently |
| Move embedder inference off event loop | M-4 | Wrap `model.encode(...)` in `asyncio.to_thread()` in `grimoire/core/embedder.py:256-263` and `:338-358`. Consider pre-loading model during startup lifespan. Add a timeout. | Concurrent `/health` requests do not block during embedding |
| Move ChromaDB calls off event loop | M-5 | Wrap all ChromaDB synchronous calls in `asyncio.to_thread()`. | API latency does not spike during vector ops |
| Decide ChromaDB access model | M-5 (cont.) | Per **Design Decision #3**: choose server-mode `HttpClient` (single writer container) or serialize writes. Document multi-process limitation if keeping `PersistentClient`. | Decision documented in `docs/DESIGN.md` or `docs/deploy/` |
| Fix query cache key | M-1 | Include `top_k`, `max_context_chunks`, LLM model, and temperature in the cache key. Normalize query text. | `top_k=5` and `top_k=100` for the same query do not collide |
| Invalidate cache on delete/re-ingest | M-1 (cont.) | Publish an event or call a cache-invalidating helper from REST delete, MCP delete, and re-ingest paths. | Deleted document's old query results are not served within the TTL window |
| Add cache variance & invalidation tests | M-1 (cont.) | Tests cover `top_k` key variance and delete invalidation. | Tests pass |
| Add bounded pagination | M-6 | Add `Query(ge=0)` to `offset`; cap `limit` with `Query(le=250)` (or `le=1000`). | Negative offset returns 422; limit > cap returns 422 |
| Fix REST `/watch` endpoints | M-7 | Per **Design Decision #2**: either (A) wire `set_watcher()` during API lifespan with a Postgres-backed state store, or (B) remove REST watch routes and update TUI/README to use CLI/MCP watch only. | Routes either work or are gone; docs are consistent |
| Fix CLI `grimoire watch unwatch` | M-8 | Per chosen architecture: persist watches to Postgres and have `unwatch` write a stop flag, or remove standalone `watch start` and move management into API. | `unwatch` successfully stops an active watch or the command is removed |

---

## Phase 5 — Minor / Polish (2–3 days)

**Theme**: Tighten input validation, fix semantic bugs, and increase test coverage.

| Task | ID | Details | Acceptance Criteria |
|------|-----|---------|---------------------|
| Escape ILIKE pattern in MCP JSONB fallback | F-6 | Reuse `documents.py:_escape_like` logic for `%`, `_`, `"` in `mcp/tools.py:416-423`. | Query with `%` in a term does not over-match on SQLite/dev |
| Tighten CORS default | F-8 | Default `allow_origins` to `["http://localhost:8001"]`; require explicit opt-in for `["*"]`. | New deployments are not wide-open by default |
| Decide `api.secret_key` fate | F-9 | Per **Design Decision #6**: implement JWT/signed URLs or remove the field and update `.env.example`. | Field either has a consumer or does not exist |
| Add rate limiter to MCP `/health` | F-11 | Reuse `create_health_limiter()` in `create_mcp_app`. | MCP `/health` returns 429 after exceeding threshold |
| Add `max_length` to query inputs | F-12 | Add `min_length=1, max_length=4000` to `QueryRequest.query` and `SearchRequest.query`. | Oversized queries return 422 |
| Fix `alembic.ini` committed URL | F-13 | Replace with placeholder `driver://user:pass@localhost/dbname` plus comment pointing at `env.py`. | No real credentials in committed `alembic.ini` |
| Harden secret placeholder validator | Secret placeholder | Reject any value containing `change-me` case-insensitively; leave example empty/commented. | `your-secret-key-change-me-in-production` fails validation |
| Optional: flatten auth timing oracle | Q-1 | Add Redis/cache lookup layer to `grimoire/api/auth.py` (already noted as TODO) or document the known tradeoff. | Timing variance reduced or documented |
| Document MCP tier ContextVar safety | Q-3 | Add comment explaining per-task asyncio isolation; add concurrent test verifying different tier values. | Test passes with two concurrent SSE requests |
| Remove or wire dead chunk overlap | m-1 | Remove `_merge_splits_with_overlap` and misleading docstring, or wire `chunk_overlap` into `_recursive_split()`. | Configured overlap actually affects output |
| Fix `$contains` operator semantics | m-2 | Rename operator to match `$in` behavior, or implement true membership/substring check. | Operator behavior matches its name |
| Fix metadata comma mangling | m-3 | Only split values originally stored as lists; stop treating scalar comma strings as CSV. | Metadata with commas round-trips correctly |
| Fix `LocalStorageAdapter.watch()` | m-5 | Either start the observer inside `watch()` or remove the method if `WatchManager` is canonical. | No unbounded `_observers` growth |
| Fix MOVED/DELETED event semantics | m-6/m-7 | Per **Design Decision #7**: decide authoritative handler, fix field mapping, and add deletion handling if supported. | Events map consistently; tests verify behavior |
| Fix `DiskCache` sync I/O and error handling | m-8 | Move diskcache calls to `to_thread()`. Scope `clear()` to namespace prefix. Symmetrize error handling (log + raise consistently). | Async methods do not block on SQLite; `clear()` only affects intended namespace |
| Fix `initialize_db()` engine leak | m-9 | Call `_db_manager.engine.dispose()` before replacement, or make manager a singleton refusing re-init. | Re-invocation does not leak asyncpg pools |
| Standardize duration suffixes | m-10 | Use `mo` for months, `m` for minutes. Parse ISO dates with `datetime.fromisoformat(...).replace(tzinfo=UTC)` when naive. | `docs.py` and `keys.py` use consistent suffixes; naive dates compare correctly |
| Two-phase vector commit | m-11 | Stage vector changes, commit DB, then finalize; on rollback, revert staged changes. Or use compensating transactions. | DB rollback leaves vector store consistent |
| Escape Redis password in URL | m-13 | Build URL with `urllib.parse.quote` or use redis client kwargs. | Passwords containing special chars do not break parsing |
| Fix MCP lifespan lazy watcher | m-14 | Check sentinel before calling `tools._get_mcp_watcher()` in `finally`; only stop if created. | Lifespan shutdown does not instantiate a watcher just to stop it |
| Add `/readyz` probe | No `/readyz` | Add authenticated `/readyz` that pings Postgres, Chroma, and Redis; keep `/health` shallow. | `/readyz` returns 503 when a dependency is down; `/health` remains fast |
| Wire or remove `GRIMOIRE_API__WORKERS` | `WORKERS` ignored | Pass `--workers ${GRIMOIRE_API__WORKERS:-1}` to uvicorn in compose, or remove the env var/docs claim. | Env var either controls worker count or does not exist |
| Add watcher healthcheck | Watcher no healthcheck | Add lightweight healthcheck to `docker-compose.yml` watcher service (touch-file timestamp or process check). | `docker compose ps` shows watcher healthy when running |
| Add tests for untested modules | Untested infrastructure | Unit tests for `grimoire/utils/token_crypto.py` (Fernet round-trip, key permissions) and `grimoire/utils/observability.py` (decorator stack). | Tests run and pass in CI |

---

## Resolved Design Decisions

| # | Decision | Resolution | Impact on Plan |
|---|----------|------------|----------------|
| 1 | Document deletion lifecycle | Cascading delete: deleting a document also wipes its wiki pages, cached query results, summaries, and flashcards. Watcher handles `DELETED` events to auto-remove files that disappear from disk. Hybrid search filters out orphaned ChromaDB chunks as defense-in-depth. | Phase 1 vector deletion + Phase 4 cache invalidation must cascade to derived content. Watcher `DELETED` handling is required. |
| 2 | Watch service architecture | REST `/watch` surface is functional and stays. `grimoire watch start` remains a standalone long-running process. Watch state must be persisted to Postgres so REST endpoints can manage it and `unwatch` can signal the daemon. | Phase 4: wire `set_watcher()` in API lifespan with Postgres-backed state store; fix CLI `unwatch` by writing a stop flag. |
| 3 | Vector-store access model | Standardize on ChromaDB server mode (`HttpClient`) with a single container. The embedder becomes a separate microservice. | Phase 4: replace `PersistentClient` with `HttpClient`; add compose service for ChromaDB server. Embedder moves to its own service/container. |
| 4 | Celery / task-queue future | **Remove Celery.** The standalone watcher daemon + embedder microservice handle background work. Re-introduce only if needs grow beyond that. | Phase 3: delete Celery dependency, compose env vars, `CeleryConfig`, and docs references. |
| 5 | Observability strategy | Add OpenTelemetry dependencies and instrumentation. Implement `LOGGING__USE_JSON` as a JSON loguru sink. | Phase 3: add `opentelemetry` deps + instrumentation. Phase 5: wire JSON sink in `setup_logger()`. |
| 6 | Security-filter model unification | Indexed columns (`severity`, `cve_id`, `mitre_technique_id`) are canonical. Remove the JSONB fallback in MCP tools once all security sources populate the indexed columns. | Phase 5: remove JSONB fallback from `mcp/tools.py`; migrate any remaining JSONB queries to indexed columns. |
| 7 | Event semantics for moved/deleted files | `path` is authoritative for MOVED events. MOVED triggers re-indexing and a rename update (not ignore). `DELETED` triggers removal. | Phase 5: fix field mapping in handlers; wire MOVED to re-index + rename. |

---

## Dependency Graph

- **Phase 1** (Critical) has no prerequisites beyond Phase 0.
- **Phase 2** (Security) should follow Phase 1 because data-deletion fixes are useless if the datastore is trivially reachable.
- **Phase 3** (Operational) should land before Phase 4 so CI runs catch regressions in the correctness fixes.
- **Phase 4** (Correctness) depends on Design Decisions 1–3 and 6–7.
- **Phase 5** (Polish) is independent once Design Decisions are resolved.

---

## Estimated Total Effort

| Phase | Days |
|-------|------|
| 0 — Prerequisites | 0.5 |
| 1 — Critical | 1–2 |
| 2 — Major Security | 1–2 |
| 3 — Major Operational | 1–2 |
| 4 — Major Correctness | 3–5 |
| 5 — Minor / Polish | 2–3 |
| **Total** | **~9–15 days** |

This is wall-clock engineering time for someone familiar with the codebase. Review and QA time is additional.

---

## How to Use This Plan

1. **Resolve Design Decisions** in `docs/DESIGN.md` or as ADRs before starting Phase 4.
2. **Open one PR per phase** (or one PR per task for large phases) to keep reviews focused.
3. **Update this plan** if scope changes — strike completed items, add discovered issues, and adjust estimates based on actual velocity.
4. **Re-run the audit checklist** after Phase 5 by re-reading the cited files to confirm every finding is addressed.
