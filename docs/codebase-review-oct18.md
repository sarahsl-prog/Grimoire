# Codebase Review Report — Grimoire

**Date**: 2026-10-18
**Scope**: Full multi-pass review — structure, security, code quality, correctness, operational readiness
**Method**: Five passes per the codebase-review skill; findings verified against source where marked.

---

## Metadata

- **Project**: Grimoire — agent-based knowledge management system
- **Type**: CLI, REST API, MCP server, GUI (PySide6), TUI (Textual), file-watcher daemon
- **Stack**: Python 3.12+, FastAPI, SQLAlchemy 2 (async) + asyncpg + Alembic, ChromaDB, Redis/DiskCache, LangChain + Ollama, Docling, pydantic-settings, loguru, slowapi, bcrypt; Docker Compose deployment
- **LOC**: ~35,200 source (141 files) / ~41,200 tests (94 files, ~2,347 test functions)
- **Repo**: `github.com/sarahsl-prog/Grimoire`, MIT license

---

## Executive Summary

Grimoire is a well-engineered personal knowledge base with unusually strong code hygiene: strict mypy, near-zero dead code, behavioral tests exceeding source in volume, tiered bcrypt-hashed API-key auth, and a shared security choke-point (`path_guard.py`) for all ingest surfaces. However, this review found **one critical correctness bug** (document deletion never removes vectors — the cleanup import targets a module that doesn't exist), **a broken MCP `pg_query` tool** one naive fix away from a write primitive, **an entirely dead REST `/watch` surface**, **event-loop blocking** in the query hot path, and **Docker Compose defaults that publish unauthenticated Postgres/Redis/Chroma on all interfaces** — significant because the security overlay targets a *public* Hetzner box. There is **no CI pipeline at all**; every quality gate depends on local pre-commit.

---

## High-Priority Findings

### CRITICAL — C-1: Document deletion never removes vectors (dead import silently swallowed)

`grimoire/api/routes/documents.py:346` and `grimoire/mcp/tools.py:1100` both do:

```python
from grimoire.services.vector_store import get_vector_store_service
...
except ImportError:
    logger.debug("Vector store service not available, skipping vector cleanup ...")
```

`grimoire/services/` **does not exist** in the package. The import always raises `ModuleNotFoundError`, which is swallowed as "not available", so vector cleanup is dead code on both `DELETE /api/v1/documents/{id}` and the MCP `grimoire_delete_document` tool. Deleted documents keep appearing in search results and as LLM context indefinitely — hybrid search serves ChromaDB content without re-validating against Postgres.

Combined with the watcher ignoring DELETED events (`grimoire/agents/watcher.py:312-317`), there is **no working mechanism at all** to remove a deleted file's chunks. For the security corpus, retired Sigma rules and CVEs remain searchable forever.

**Fix**: implement the module (or call the existing `ChromaDBStore` directly) and stop swallowing the ImportError; add an integration test that deletes a document and asserts the vector is gone.

*Verified: `grimoire/services/` is absent from the package; both import sites confirmed.*

### MAJOR — F-4: MCP `pg_query` is broken AND a latent write bypass

`grimoire/mcp/tools.py:1117-1144`:

```python
sql = text(
    "SELECT * FROM (:inner_query) AS _grimoire_q LIMIT :row_limit"
).bindparams(inner_query=inner, row_limit=params.limit)
```

1. User SQL is passed as a *bind parameter* in a FROM position — invalid in Postgres, so **every call fails** with a syntax error (all tests mock `db.execute`, hiding this).
2. The "must be SELECT" validator (`tools.py:352-374`) is a string-shape check; a data-modifying CTE passes it:

   ```sql
   WITH x AS (DELETE FROM documents RETURNING *) SELECT * FROM x
   ```

   Only bug (1) keeps it inert — the first naive "fix" interpolating the SQL turns it into a write/DDL primitive. There is no DB-level read-only enforcement.
3. `return _err(f"Query failed: {e}")` surfaces the raw DB exception to the MCP client, contrary to the project's own error-handling standard.

**Fix**: connect the tool's session to a Postgres role with SELECT-only grants (or `SET TRANSACTION READ ONLY` + a statement timeout), regex-reject data-modifying CTE keywords (`DELETE|UPDATE|INSERT|MERGE|ALTER|CREATE|GRANT|COPY`), and log exceptions server-side while returning a generic message.

### MAJOR — F-1/F-2/F-3: Compose publishes unauthenticated datastores (public-Hetzner target)

*Verified against `docker-compose.yml` directly.*

- **F-1 — Redis, no password, all interfaces** (`docker-compose.yml:86-98`):

  ```yaml
  ports:
    - "${REDIS_PORT:-6379}:6379"
  command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
  ```

  No `--requirepass` anywhere. On the public deployment this file targets (`.env.security.example` / `docs/deploy/hetzner_security.md`), anyone on the internet gets unauthenticated Redis access: cache poisoning, rate-limit-bucket wipe, `ALLKEYS-LRU` eviction.

- **F-2 — Postgres, weak fallback password, all interfaces** (`docker-compose.yml:50-54`):

  ```yaml
  POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-grimoire}
  ports:
    - "${POSTGRES_PORT:-5434}:5432"
  ```

  If `.env` is absent, Postgres runs with the password `grimoire` published on all interfaces. The security overlay inherits this default (`docker-compose.security.yml:27`) and adds another published Postgres port (`:31`).

- **F-3 — ChromaDB, no auth, full corpus readable** (`docker-compose.yml:101-112`, also `docker-compose.security.yml:41-50`): the Chroma server is published with no auth token configured — anyone who can reach the host port can read or delete the entire embedded corpus, including the security-domain corpus (CVE/Sigma/playbooks).

The only mitigation is a documented, optional `ufw` step (`docs/deploy/hetzner_security.md:80-84`).

**Fix**: bind infrastructure ports to loopback (`"127.0.0.1:..."`), add `--requirepass ${REDIS_PASSWORD}` (wired into `GRIMOIRE_REDIS__PASSWORD`), set `CHROMA_SERVER_AUTHN_TOKEN`, remove the Postgres password fallback (compose fails fast if unset), and have the security overlay drop infrastructure `ports:` entirely.

### MAJOR — M-7/M-8: The REST watch surface and `watch unwatch` are dead

- **M-7** — `grimoire/api/routes/watch.py:27-42`: module-global `_watcher` is set only by `set_watcher()`, which has **zero callers** repo-wide, and the 503 error text references a `--watch` flag that does not exist in any server entrypoint. Every `POST /api/v1/watch/start`, `DELETE /api/v1/watch/{id}`, and `GET /api/v1/watch/status` permanently returns 503. The TUI and README document these endpoints; the MCP server wires its own watcher (`mcp/tools.py:75-79`) and works — so the two surfaces diverge silently.
- **M-8** — `grimoire/cli/watch.py:113-147`: `grimoire watch unwatch` constructs a brand-new empty `WatchManager` in its own process, so `stop_watch` always misses. The only way to stop a watch is killing the `watch start` process, contradicting its own help text.

**Fix**: wire `set_watcher()` in the app lifespan (or delete the REST routes), and make watch state durable (DB-backed) so `unwatch` can act cross-process.

### MAJOR — M-4/M-5: Sync inference and ChromaDB calls block the event loop; multi-process ChromaDB races

- **M-4** — `grimoire/core/embedder.py:256-263`: `async def embed` runs fully synchronous sentence-transformers inference — and on first call, a model **download + load** (tens of seconds) — with no `run_in_executor`/`to_thread`. Called from `/query/ask`, `/query/search`, and `/ingest/*`, it freezes the entire event loop, including `/health`. The Docling parser does this correctly (`parser.py:516-521`); the embedder does not.
- **M-5** — `grimoire/vectorstore/chromadb.py`: all ChromaDB operations (sync SQLite/HNSW) run inline in async endpoints (lines 232, 333, 493, 526, 595, 625), and `PersistentClient` is single-process by design while the API server, `grimoire watch start`, and `grimoire ingest` each open the same `chroma_db/` directory → `database is locked` errors, dropped writes, or index corruption. No serialization layer exists.

**Fix**: wrap embedder and ChromaDB calls in `to_thread`; move to a single writer process or Chroma server-only mode for multi-process deployments.

---

## Security Findings

### MAJOR

- **F-5 — Tier rate limits are dead config**. `get_tier_rate_limit` / `DEFAULT_TIER_RATE_LIMITS` (`grimoire/api/rate_limit.py:52-54`) and `settings.auth.rate_limits` (600/120/30 per minute) are never applied — no route has a tier-specific limit; SlowAPIMiddleware applies only the flat 30/min default to every tier. Fail-closed (agent keys throttled 20x below spec), but the documented tier limits are not implemented. *Fix*: slowapi callables that read `request.state.api_key.tier`, or delete the config.
- **F-7 — `pg_query` can read the `api_keys` table at DEV tier** (`grimoire/mcp/tools.py:1117`, `db/models.py:1192`): a DEV key can dump every key's `key_prefix`, bcrypt `key_hash`, and name. Offline-cracking the 32-byte random part is infeasible, but key inventory/metadata leaks to a lower tier than intended; no statement timeout means `pg_sleep(...)` DoS is unblocked. *Fix*: SELECT-only role scoped to read tables, `SET LOCAL statement_timeout`.
- **Ops — No CI pipeline, unpinned Chroma image**. No `.github/workflows/` or any other CI config exists — lint/mypy/bandit/tests run only via local pre-commit; no security scanning (trivy/dependabot/pip-audit). `chromadb/chroma:latest` (`docker-compose.yml:102`) is unpinned — a schema-changing pull can silently break persisted vectors.

### MINOR

- **F-6 — Unescaped ILIKE wildcards in JSONB fallback** (`mcp/tools.py:402-423`): `cast(column, String).ilike(f'%"{value}"%')` interpolates caller-controlled `%`/`_`. Not SQL injection (bound), but over-matches on SQLite/dev; Postgres uses the `@>` path. *Fix*: escape like `documents.py:_escape_like`.
- **F-8 — CORS default is `allow_origins=["*"]`** (`config/settings.py:807-810`): low risk (header auth, `allow_credentials=False`), still default-open. *Fix*: default to localhost, require opt-in.
- **F-9 — `api.secret_key` is validated but never used** (`config/settings.py:742-791`): operators are told to set a strong secret; nothing consumes it. *Fix*: remove or document as reserved.
- **F-11 — Standalone MCP `/health` unrate-limited** (`mcp/app.py:63-65`) vs. the API's dedicated 60/min limiter (`api/main.py:196-208`).
- **F-12 — Inconsistent input caps**: REST `QueryRequest.query` has no `max_length` (`api/schemas.py:104-107`) while MCP caps at 2000-4000 chars.
- **F-10 — `old.env.bak`** with a real DB password sits at repo root. Gitignored and never committed (verified), but a stray plaintext secrets backup is a hygiene leak via editors/sync. *Delete it.*
- **F-13 — `alembic.ini:8`** carries dev credentials (`grimoire:grimoire@localhost`). Inert (`alembic/env.py` overrides), but normalizes weak creds in a committed file.
- **Secret placeholder validator bypass**: `settings.py:779-791` rejects only the exact token `change-me-in-production`; the shipped example value `your-secret-key-change-me-in-production` (`env.example:159`) passes. *Fix*: reject anything containing "change-me" case-insensitively.

### QUESTION

- **Q-1 — Prefix-lookup timing oracle** (`api/auth.py:77-94`): a matching 12-char prefix pays a ~100ms bcrypt check; a miss returns fast. Reveals key *existence*, never key material (remaining ~43 chars stay secret). Standard tradeoff of prefix-indexed bcrypt keys; the planned Redis lookup cache would flatten the timing too.
- **Q-3 — ContextVar-based MCP tier context** (`mcp/auth_stdio.py:23`, `router.py:118-120`): safe as written (per-request task isolation), but the safety is implicit. A comment or test pinning this would prevent a future refactor from sharing state globally.

### Verified fine (coverage)

1. **Key storage & generation** — bcrypt-hashed, never raw (`auth.py:61-65`); `secrets.token_urlsafe(32)` (~256 bits); prefix-only DB lookup; revoked/expiry checks. Key creation is CLI-only; `/keys/me` exposes only name/tier/prefix/dates.
2. **REST auth coverage** — every route in all 7 routers gated by `Depends(get_api_key)` or `require_min_tier(...)`; only `/health` is public (with its own limiter). `tests/test_api_tier_enforcement.py:116-144` walks every router asserting mutating routes are gated. Tier map consistent REST↔MCP.
3. **MCP auth** — SSE mounted behind ASGI middleware validating `X-API-Key` against DB per request (`router.py:38-122`); non-HTTP scopes rejected; DB-down yields 503, not a bypass. stdio requires `GRIMOIRE_API_KEY` validated at lifespan.
4. **Path traversal** — one canonical choke point (`path_guard.py`) shared by REST ingest, REST watch, MCP ingest, MCP watch: null-byte rejection, length cap, `realpath` before `is_relative_to`, canonicalized roots, empty-roots refuse everything, `/` rejected as root (`settings.py:772-776`).
5. **Upload endpoint** — `Path(...).name` + uuid4 prefix, extension whitelist, streamed size cap, pre-routing `ContentLengthGuard` 413 middleware so oversized bodies are never parsed (`main.py:40-110`).
6. **SQL** — `to_tsquery` via bind params with operator escaping (`fulltext.py:88-95`); `_escape_like` with `escape="\\"` (`documents.py:35-42`); ORM everywhere else. No f-string/`.format` near `execute`/`text()` except the two flagged sites.
7. **No code-exec vectors** — no `os.system`/`eval`/`exec`/`pickle`; the only `subprocess` is the CLI `$EDITOR` launch (argv-list, no shell). Docling parses in-process with size/timeout caps.
8. **Deserialization** — `yaml.safe_load` only; OAuth tokens Fernet-encrypted at rest with 0600 key / 0700 dir (`token_crypto.py:21-51`).
9. **No committed secrets** — `git ls-files` shows only `.env.example`/`.env.security.example` (placeholders); `.env` and `old.env.bak` never committed (verified via `git log --all`).
10. **Docker** — non-root `USER 10001`, watcher corpus mounted `:ro`.
11. **Session-ID log injection** — validated `[A-Za-z0-9_-]{1,64}` with `fullmatch` (`session_id.py:24-37`).

---

## Quality & Maintainability

The codebase substantially lives up to its own claims.

| Category | Rating | Notes |
|---|---|---|
| Type annotations | Strong | strict mypy; only 33 `type: ignore`, nearly all justified optional-dep boundaries; per-module overrides *documented with rationale* |
| Complexity | Good | 1 function >150 lines (`ingest_file`, 207 — `agents/ingestion.py:241`); `mcp/tools.py` (1,180 lines) is the god-module suspect |
| Error handling | Excellent | zero bare `except:`; one deliberate `except Exception: pass` with noqa+explanation; dominant pattern is log-with-context → chained domain error |
| Dead code | Excellent | 2 TODOs in 35K LOC; no commented-out code; no unused imports (exceptions: phantom `services.vector_store` import, dead `/watch` surface) |
| Duplication | Low | CLI/API/MCP all delegate to shared agents; GUI/TUI share `grimoire.client`; main drift risk: MCP JSONB-blob security filters vs. API indexed columns — two parallel filter mechanisms |
| Logging | Good | loguru in 56 files with context; zero production `print()`; a few API routes / MCP router lack local loggers |
| Docstrings | Excellent | 0 missing module docstrings in core/api/agents; 6 trivial public functions missing; consistent Google style; comments explain *why* |
| Tests | High | genuinely behavioral (adversarial edge cases, ASGI scope tests, unicode/large-file dedup); ~7 untested modules — `utils/token_crypto.py` (Fernet) and `utils/observability.py` matter most |
| Config | Excellent | bounded, documented Pydantic Fields; layered YAML → .env → env precedence; friendly `ConfigurationError`, never raw tracebacks |

Actionable quality targets:
1. Extract `mcp/tools.py`'s inline SQL helpers and ~350 lines of input models into separate modules.
2. Unify the security-filter mechanism (JSONB blob vs. indexed columns) between MCP and API layers.
3. Add tests for `utils/token_crypto.py` and `utils/observability.py`.

---

## Correctness Issues

Confirmed beyond the high-priority items:

- **M-1 — Query cache key omits `top_k`; never invalidated** (`agents/query.py:471-475`): key covers only query + filters. `ask top_k=100` after `top_k=5` returns the 5-chunk answer; after a document is deleted or re-ingested, stale answers citing dead/old chunks serve for the full 1h TTL (`query.py:526`).
- **M-2/M-3 — Watcher thread→loop handoff unsafe by construction** (`storage/watch_manager.py:393-399`): `asyncio.Queue.put_nowait` from the watchdog observer thread is not thread-safe — when the loop task is parked in `queue.get()`, `future.set_result` raises inside the observer thread, is swallowed, and the **event is silently dropped**. Separately, `asyncio.create_task` from the observer thread always raises `RuntimeError: no running event loop` (swallowed), so **async callbacks never fire at all** for local watches. Cloud polling works (runs on the loop). *Fix*: `loop.call_soon_threadsafe(queue.put_nowait, ...)` and thread-safe task scheduling.
- **M-6 — Unvalidated pagination** (`api/routes/documents.py:74-75`): `?offset=-5` → Postgres syntax error → 500 with a raw DB error; `?limit=999999999` dumps the table. *Fix*: `Query(ge=0, le=...)`.
- **m-1 — Configured `chunk_overlap` is dead code** (`core/chunker/recursive.py:153-205`): `_merge_splits_with_overlap` has zero callers; the docstring claims overlap is applied. `chunk_overlap` from settings is a no-op for this chunker.
- **m-3 — ChromaDB metadata round-trip mangles comma-containing strings** (`vectorstore/chromadb.py:461-464`): read-side splits every comma-containing string into a list — a title "RAG, an overview" comes back as `["RAG", "an overview"]` in search-result metadata.
- **m-2 — `$contains` silently becomes exact-match `$in`** (`chromadb.py:384-389`): a tag stored as `"research,ml"` won't match `$contains: "research"` — false negatives on security filters.
- **m-10 — `--since` timezone/unit inconsistencies**: ISO dates parse naive (server session TZ) while relative units are UTC-aware (`cli/docs.py:42-45`, `cli/keys.py:40-42`); `"m"` means minutes in `keys.py` but 30-day months in `docs.py`.
- **m-12 — Root-level `app.py` + `rag_pipeline.py` are a drifted prototype** — a standalone Streamlit + FAISS stack using `FAISS.load_local(..., allow_dangerous_deserialization=True)`, different models, and bypassing every package control (path guard, dedup, auth). Delete or move to `examples/`.
- **m-5/m-6/m-7** — `LocalStorageAdapter.watch()` logs "Started watching" but never calls `observer.start()`; MOVED-event `previous_path`/`path` semantics are inverted between the two watchdog handlers; DELETED events are dropped by the watcher (undocumented scope limitation that compounds C-1).
- **m-8** — `DiskCache.clear()` wipes all namespaces (a query-cache clear also nukes the embedding cache); sync SQLite IO in async methods; `get` swallows all errors while `set` raises (asymmetry hides corruption).
- **m-9/m-11/m-13/m-14** — `initialize_db` re-invocation leaks the previous engine pool; ingestion deletes old vectors before DB commit and upserts new vectors before commit (no transactional ordering vs. ChromaDB); Redis URL doesn't escape passwords with special chars (`rate_limit.py:77-79`); MCP shutdown lazily *constructs* a watcher just to stop nothing (`mcp/server.py:57-64`).

Checked and found sound: DB session handling (pooled engine via lifespan, `pool_pre_ping`, context-managed sessions), pagination filter math (only bounds missing), ContextVar key isolation, hash-based dedup for duplicate watch events, Docling's correct `run_in_executor` + timeout.

---

## Operational Concerns

**Overall posture**: container and deployment hygiene is well above average — multi-stage pinned Dockerfile running as non-root, healthchecks on every stateful service, a one-shot `db-migrate` service correctly gating every application container via `service_completed_successfully`, reproducible deps via `uv sync --frozen`, and an unusually good UPGRADING.md with real rollback/restore procedures. The significant gaps are operational.

- **No CI/CD** — no `.github/`, no GitLab/Jenkins/CircleCI config; all gates run only via local pre-commit. `tests/deploy/` is already CI-friendly (text-parsing, no daemon needed). *Fix*: one workflow — `uv sync`, pytest, `pre-commit run --all-files`, trivy/grype image scan.
- **Phantom config surface**:
  - **Celery** is a *hard* runtime dependency (`pyproject.toml:41`) with compose wiring (`GRIMOIRE_CELERY__*`), 7 documented env vars, and a validated `CeleryConfig` (`settings.py:360-394`) — and **zero imports** anywhere in `grimoire/`, plus no worker service defined.
  - **OTEL/tracing/metrics** switches (`settings.py:600-601`, `.env.example:145-149`) and **`LOGGING__STRUCTURED`/`USE_JSON`** (`settings.py:339-340`) are never read by any code.
  - This misleads operators about what the system actually does. *Fix*: implement (the JSON sink is a ~5-line change in `utils/logger.py`) or delete.
- **Duplicate `dev` dependency definitions** — `[project.optional-dependencies].dev` (pyproject.toml:69-88) vs `[dependency-groups].dev` (pyproject.toml:317-323) with conflicting floors:

  | Package | extra | group |
  |---|---|---|
  | pytest-asyncio | >=0.23 | >=1.3.0 |
  | pytest-cov | >=4.1 | >=7.1.0 |
  | pytest-httpx | >=0.30 | >=0.36.2 |
  | ruff | >=0.3 | >=0.15.8 |

  The two also define different tool sets; `uv sync --extra dev` and `--group dev` produce materially different environments, and pytest-asyncio 0.23 vs 1.3 is a behavior-changing gap. *Fix*: delete one (keep the uv-native group) or align floors.
- **No readiness probe** — `/health` on both apps is deliberately shallow (documented: liveness must survive Redis-down), but no `/readyz` checks Postgres/Chroma mid-flight, so Docker reports healthy during an outage.
- **`GRIMOIRE_API__WORKERS` not honored under Docker** — compose runs bare `uvicorn` with no `--workers` flag; single worker regardless of the setting.
- **Watcher service has no healthcheck** — `restart: unless-stopped` + no probe means a wedged watcher looks "Up" forever.

**Done well (ops)**: graceful shutdown (signal handlers via `loop.add_signal_handler`, awaited task cancellation, DB pools closed in lifespan `finally`); loguru with rotation/retention/compression and `enqueue=True`; session-ID on every log record; MLflow tracing as a clean optional import; linear Alembic chain 0001→0007 with downgrades; `backups/` gitignored and excluded from the build context; `docs/UPGRADING.md` is exemplary (backup verification, rollback via `alembic downgrade` or volume restore, embedding-model-change caveats).

---

## Recommendations

Prioritized, most important first:

1. **Fix vector deletion (C-1)** — implement `grimoire/services/vector_store.py` or call `ChromaDBStore` directly; stop swallowing the ImportError; add a delete→vector-gone integration test.
2. **Harden Compose defaults (F-1/F-2/F-3)** — loopback bindings, Redis password, Chroma auth token, remove the Postgres password fallback; especially in the *security* overlay for the public Hetzner deploy.
3. **Rebuild `pg_query` (F-4/F-7)** — read-only DB role, statement timeout, CTE-keyword rejection, generic client errors.
4. **Add CI** — one workflow: `uv sync`, pytest, `pre-commit run --all-files`, image scan. Zero CI is the root of most enforcement gaps.
5. **Fix the watcher thread handoff (M-2/M-3)** with `loop.call_soon_threadsafe`; wire or remove the dead REST `/watch` + CLI `unwatch` surface.
6. **Move embedder/ChromaDB work off the event loop** (`to_thread`) and serialize multi-process ChromaDB access (single writer process or server-only mode).
7. **Prune the phantom surface** — remove Celery + OTEL + `USE_JSON` config or implement them; reconcile the duplicated dev-dependency groups; delete `app.py`/`rag_pipeline.py`; delete `old.env.bak`; wire `set_watcher`.
8. **Cache correctness (M-1)** — include `top_k`/model in the cache key; invalidate on delete/update. Add pagination bounds (M-6).
9. **Polish** — tests for `utils/token_crypto.py` and `utils/observability.py`; add `/readyz`; pin `chromadb/chroma`; wire or delete tier rate limits; fix `--since` timezone/unit handling; escape ILIKE wildcards in the MCP fallback path.

---

## Positive Findings

- **Path traversal handled by one canonical, well-tested choke point** (`path_guard.py`) shared by every ingest surface — null bytes, symlinks, length, root canonicalization.
- **Auth done right** — bcrypt-hashed ~256-bit keys, tier gates consistent across REST and MCP, and *test-enforced* route coverage (`tests/test_api_tier_enforcement.py`).
- **Tests are adversarial and behavioral**, not smoke tests — e.g. `test_upload_unlinks_staged_copy_when_agent_result_is_skipped`, ASGI-middleware scope tests, unicode/large-file dedup edges. Test LOC exceeds source LOC.
- **Deploy configs are statically tested** (`tests/deploy/` asserts non-root user, compose overlay behavior, Dockerfile secrets) — rare discipline.
- **Comments explain *why*** throughout; every lint/type/security ignore is individually justified; per-file-ignores carry rationale comments.
- **Graceful shutdown, migration gating, and log hygiene are production-grade**; UPGRADING.md documents real rollback and restore procedures.

---

## Verdict

- **Verdict: ship with fixes** — engineering discipline is genuinely high (auth, tests, docs, error handling), but the broken vector deletion, the live `pg_query` landmine, the dead watch surface, and the public-facing Compose defaults need to be fixed before this touches a real deployment, and the absence of CI means none of the project's own high standards are enforced.

*Note: findings C-1 (missing `grimoire/services`), the dead `set_watcher`, and the Compose port/password defaults were independently re-verified against the source before this report.*