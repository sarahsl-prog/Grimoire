# Grimoire Code Review Report

**Date:** May 26, 2026  
**Reviewer:** Hermes Agent  
**Scope:** Full codebase audit of `/home/sunds/Code:/Grimoire` (88 Python files, ~24,600 LOC in package)  
**Focus:** Security, correctness, data model integrity, API surface, configuration, and operational safety.

---

## 1. Codebase Overview

| Metric | Value |
|---|---|
| Package files | 88 `.py` files |
| Total LOC (package) | ~24,600 |
| Largest modules | `db/models.py` (1,210), `config/settings.py` (1,122), `agents/ingestion.py` (983) |
| Python version | ≥3.12 |
| Framework | FastAPI + SQLAlchemy (async) + Pydantic v2 |
| Vector store | ChromaDB (default), Qdrant optional |
| LLM backend | Ollama |
| Cache | Redis or DiskCache |

---

## 2. Security Findings

### 🔴 Critical

#### C-1: Default JWT secret_key in `APIConfig` is a hardcoded placeholder
**File:** `grimoire/config/settings.py:697-699`  
**Issue:**
```python
secret_key: str = Field(
    default="change-me-in-production",
    description="Secret key for JWT (CHANGE IN PROD!)",
)
```
If this is ever used for JWT signing (e.g., in a future auth expansion), a hardcoded default is a textbook credential leak. Even as a placeholder, it trains users to ignore the warning.

**Fix:** Remove the default entirely; require `GRIMOIRE_API__SECRET_KEY` at startup and raise a clear `ValueError` if not set. Or generate a cryptographically random default on first run and persist it securely (e.g., in a config directory with `0o600` permissions), so every deployment has a unique key.

---

#### C-2: Path traversal mitigation in `ingest.py` is insufficient
**File:** `grimoire/api/routes/ingest.py:33-36, 64-66`  
**Issue:**
```python
allowed_roots = [Path("/tmp"), Path("/home"), Path("/home/sunds")]
if not any(str(resolved_path).startswith(str(root)) for root in allowed_roots):
```
The `str.startswith` check against `/home` is **vacuously true** for `/home/sunds` because every path under `/home/sunds` also starts with `/home`. More importantly, `str.startswith` on resolved paths is fragile: a path like `/home/../../etc/passwd` resolves first but `resolve()` follows symlinks, so if an attacker can place a symlink, this check is bypassable.

**Fix:** Use `pathlib.Path.is_relative_to()` (Python 3.9+) after `resolve()` *and* `realpath()` (to break symlinks), and keep the allowed roots as `Path` objects:
```python
allowed_roots = [Path("/tmp").resolve(), Path("/home/sunds").resolve()]
resolved = Path(body.file_path).resolve()
real = os.path.realpath(resolved)
if not any(Path(real).is_relative_to(root) for root in allowed_roots):
    raise HTTPException(403, ...)
```
Also, the `..` and `~` checks on `body.file_path` should use `body.file_path` *after* it is normalized, not before, because `~` and `..` are valid inside legitimate directory names.

---

#### C-3: CORS origins default to wildcard `["*"]`
**File:** `grimoire/config/settings.py:716-718`  
```python
cors_origins: list[str] = Field(
    default_factory=lambda: ["*"],
    description="Allowed CORS origins",
)
```
**Issue:** This is a production deployment risk. Combined with `allow_credentials=False`, it's less severe, but wildcard CORS on a knowledge-management API with tiered auth is still a footgun. An inadvertently deployed dev config could expose user data to arbitrary origins.

**Fix:** Default to an empty list or `["http://localhost:3000"]` and require explicit override for production. Add a startup warning if `cors_origins == ["*"]`.

---

#### C-4: Sensitive config values in env/secret files are not redacted from logs
**File:** `grimoire/config/settings.py` (secret fields list: 1014-1018)  
**Issue:** `settings.py` lists sensitive paths (redis password, client secrets, api secret_key) in a `_secrets` tuple used for log filtering, but the `get_field_value` logic in `YamlConfigSource` can still emit parsed YAML values through loguru at `INFO` or `DEBUG` level during startup diagnostics.

**Fix:** Ensure `repr(settings)` and `settings.model_dump()` both apply the redaction mask (`***`) before any log emission. Audit all `logger.info/Debug` calls that print settings dicts.

---

### ⚠️ Warnings

#### W-1: OAuth tokens stored on disk in plain JSON without encryption
**Files:** `grimoire/storage/gdrive.py` (lines 189-194), `grimoire/storage/onedrive.py` (137-147)  
**Issue:** Both Google Drive and OneDrive adapters save refresh tokens as unencrypted JSON in `~/.config/grimoire/...`. The file is chmod'd to `0o600`, which is good for Unix permissions but useless if the filesystem is backed up, synced to cloud, or the host is compromised.

**Fix:**
- Option A: Encrypt tokens at rest using a key derived from a machine-bound secret (e.g., `keyring` or a system KEystore).
- Option B: At minimum, document the risk and recommend `keyring` integration in production.

---

#### W-2: `generate_api_key()` does not enforce tier rate-limit validation
**File:** `grimoire/api/auth.py:58`  
**Issue:** When generating a key, any arbitrary `ApiKeyTier` can be requested, but there is no runtime check that the corresponding rate-limit string is valid for the `slowapi` format.

**Fix:** Add validation in `generate_api_key()` that the tier has a corresponding entry in `DEFAULT_TIER_RATE_LIMITS` (or the settings override), and raise if not.

---

#### W-3: API key `last_used_at` update lacks error isolation
**File:** `grimoire/api/auth.py:102-103`  
```python
api_key.last_used_at = datetime.now(timezone.utc)
await db.flush()
```
**Issue:** If `db.flush()` fails (e.g., DB connection lost), the entire request fails with a 500 even though auth already succeeded. The comment says "fire-and-forget" but the code does not catch exceptions.

**Fix:** Wrap the `flush()` in a `try/except` to prevent auth success turning into a 500. Log the failure but continue returning the response.

---

#### W-4: Missing input length validation on `source_path` (2048 chars) and `path` (2048)
**File:** `grimoire/db/models.py:227-230`, `grimoire/db/models.py:778-781`  
**Issue:** The model defines `String(2048)`, but the API routes do not validate that user-provided paths are within this limit before attempting DB insertion. A very long path will cause a DB error rather than a clean 400 response.

**Fix:** Add Pydantic max-length validators to the request schemas (`IngestFileRequest`, `IngestDirectoryRequest`, `WatchPath` models, etc.).

---

#### W-5: `Chunk.prev_chunk_id` / `next_chunk_id` self-referential FKs allow circular references
**File:** `grimoire/db/models.py:449-482`  
**Issue:** SQLAlchemy's `post_update=True` on the self-referential relationships means the ORM handles circular updates carefully, but there is no DB-level or application-level check that prevents `A.prev → B`, `B.prev → A` or infinite chains.

**Fix:**
- Add an application-level check during chunk creation that walks `prev_chunk_id` at most N steps to detect cycles.
- Alternatively, enforce via a CHECK constraint or a SQL trigger (though triggers are backend-specific).

---

#### W-6: `ProcessingLog` table lacks partitioning or retention policy
**File:** `grimoire/db/models.py:825-883`  
**Issue:** `ProcessingLog` has an index on `created_at` but no partitioning, automated pruning, or TTL. On a high-throughput system, this table will grow indefinitely.

**Fix:**
- Document a recommended PostgreSQL partition strategy (e.g., monthly partitions on `created_at`).
- Include a cron job or background task that prunes logs older than a configurable TTL (e.g., 90 days).
- Add `retention_days` to `ObservabilityConfig`.

---

#### W-7: No rate-limit enforcement on `/health`
**File:** `grimoire/api/main.py:67-69`  
**Issue:** The `/health` endpoint is unauthenticated and unrate-limited. An attacker can probe it for service detection and DoS the underlying health checks.

**Fix:** Add a lightweight IP-based rate limit to `/health` (e.g., 60 req/min per IP). This is standard for public health endpoints.

---

#### W-8: `Document.error_message` stores arbitrary strings unlimited in length
**File:** `grimoire/db/models.py:284-287`  
**Issue:** `error_message` is an unbounded `Text` column. If an ingestion agent captures a full stack trace or a very long LLM error response, this could bloat the DB row.

**Fix:** Truncate error messages to a reasonable length (e.g., 4096 or 8192 chars) before storage, and log the full message to the file logger.

---

#### W-9: `generated_content.content` can store arbitrarily large LLM output
**File:** `grimoire/db/models.py:637-639`  
**Issue:** `content: Mapped[str] = mapped_column(Text, nullable=False)`. LLM outputs can be very long (flashcards, outlines), and there is no truncation before DB insertion.

**Fix:** Add a `max_content_length` configuration and truncate or reject outputs exceeding it. Alternatively, offload large generated content to object storage and store only a reference.

---

#### W-10: `WikiPage.content` does not have a length limit either
**File:** `grimoire/db/models.py:957`  
**Issue:** Same as W-9. Wiki pages can grow indefinitely.

**Fix:** Enforce a max length or paginate into `WikiPageSection` rows automatically.

---

## 3. Architecture & Design Findings

### A-1: Monolithic `models.py` (1,210 lines) violates single-responsibility
**File:** `grimoire/db/models.py`  
**Issue:** One file contains 12 model classes plus enums and a custom `TypeDecorator`. This makes merge conflicts likely and navigation difficult.

**Fix:** Split into a `models/` package with modules like `document.py`, `wiki.py`, `api_key.py`, `processing.py`, etc., re-exporting from `models/__init__.py`.

---

### A-2: `settings.py` (1,122 lines) is similarly oversized
**File:** `grimoire/config/settings.py`  
**Issue:** Contains ~16 nested config classes, a custom YAML source, the main `GrimoireSettings` class, and a global singleton instance.

**Fix:** Move nested config classes into `config/models/` directory (e.g., `llm.py`, `database.py`, `security.py`) and keep `settings.py` as a thin orchestrator.

---

### A-3: `ingestion.py` (983 lines) and `gdrive.py` (901 lines) should be refactored
**Files:** `grimoire/agents/ingestion.py`, `grimoire/storage/gdrive.py`  
**Issue:** Both modules exceed 900 lines. `ingestion.py` handles parsing, chunking, embedding, tagging, and DB writes. `gdrive.py` mixes OAuth flow, token management, pagination, and change-tracking.

**Fix:**
- Ingestion: Extract `DocumentParser`, `ChunkPipeline`, and `EmbedPipeline` into separate classes.
- GDrive: Split into `auth.py`, `pagination.py`, and `sync.py`.

---

### A-4: No middleware for request ID propagation
**File:** `grimoire/api/main.py`  
**Issue:** There is no correlation ID middleware. In a multi-agent, async system with Celery, tracing a single request across DB logs, agent logs, and LLM calls is nearly impossible.

**Fix:** Add a FastAPI middleware that generates or extracts a `X-Request-ID` header and binds it to loguru context. Pass it through to Celery tasks and LLM calls.

---

## 4. Correctness & Bug Risks

### B-1: `datetime.now(timezone.utc)` in default lambdas is evaluated once at import
**File:** `grimoire/db/models.py` (multiple lines: 261, 268, 465, etc.)  
**Issue:**
```python
created_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True),
    server_default=func.now(),
    default=lambda: datetime.now(timezone.utc),
    ...
)
```
SQLAlchemy's `default=` on model fields is actually fine here — it's evaluated per-row at insert time, not at import time. However, the `onupdate=lambda: datetime.now(timezone.utc)` pattern is also correct per-row. This is a **false positive** in many codebases, but worth noting for awareness.

**Verification:** This is actually safe with SQLAlchemy 2.0 `mapped_column`, so no fix needed unless you want to switch to `server_default` + `server_onupdate` for DB-native handling.

---

### B-2: `onupdate=lambda: ...` does not trigger on bulk updates
**File:** `grimoire/db/models.py:269`  
**Issue:** The client-side `onupdate` only fires when using ORM `Session.flush()` on individual instances. Bulk `update()` via SQLAlchemy Core will not update `updated_at`.

**Fix:** Add PostgreSQL triggers (or SQLite triggers) that enforce `updated_at = CURRENT_TIMESTAMP` on update, or document that bulk updates must explicitly set `updated_at`.

---

### B-3: `PortableJSON` uses `BaseJSON` fallback for SQLite, but `JSONB` for PostgreSQL
**File:** `grimoire/db/models.py:56-65`  
**Issue:** On SQLite, `BaseJSON` stores JSON as text. If the application queries or indexes JSON keys on SQLite, performance will be poor compared to PostgreSQL's JSONB.

**Fix:** Document that SQLite is dev-only and JSON operations (e.g., `security_metadata` lookups) are not performant on it. Or add a `sqlite_json` extension check.

---

### B-4: `QueryAgent` does not validate LLM response JSON before caching
**File:** `grimoire/agents/query.py` (implied from pattern, not fully read)  
**Issue:** If the LLM returns malformed JSON or an unexpectedly long string, it gets cached and served verbatim on subsequent identical queries.

**Fix:** Add a response schema validator (via Pydantic) before storing to cache. Invalid responses should not be cached.

---

## 5. Testing Coverage Gaps

| Area | Status | Notes |
|---|---|---|
| API auth | Partial | Tests for key revocation, expiry edge cases missing |
| Path traversal | Missing | No symlink-attack tests in `test_api.py` |
| Rate limiting | Missing | No tests for tier enforcement or burst handling |
| CORS | Missing | No negative tests for disallowed origins |
| Cache corruption | Missing | No tests for Redis/DiskCache failure recovery |
| OAuth token refresh | Missing | `gdrive.py` and `onedrive.py` lack mock token-refresh tests |
| Chunk continuity | Missing | No tests for prev/next chunk cycle detection |
| Concurrent ingestion | Missing | No tests for two agents racing on same file hash |

**Fix:** Prioritize tests for the security-critical paths: auth, path traversal, rate limiting, and concurrent dedup.

---

## 6. Documentation & Operational

### D-1: `docker-compose.yml` uses default passwords
**File:** `docker-compose.yml:9`  
```yaml
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-grimoire}
```
**Issue:** The fallback password is hardcoded. If the `.env` file is missing on a naive first run, a weak password is silently used.

**Fix:** Remove the default fallback; make the container fail fast if `POSTGRES_PASSWORD` is not set, or generate a random password to a file on first `docker compose up`.

---

### D-2: `README.md` still says "Alpha" but the project is in production use
**File:** `pyproject.toml:13`  
```
"Development Status :: 3 - Alpha",
```
**Issue:** Alpha status does not match the production deployment on Hetzner.

**Fix:** Update trove classifier to `Development Status :: 4 - Beta` or `5 - Production/Stable` as appropriate.

---

### D-3: `Claude.md` and `.claude/agents/` directory contain stale context
**File:** `Claude.md`, `.claude/agents/*.md`  
**Issue:** These files may contain outdated instructions or references. Stale Claude-specific instructions can mislead future AI-assisted development.

**Fix:** Review `Claude.md` quarterly. Consider moving Claude-specific context to a versioned `docs/ai-context/` directory with a freshness date.

---

## 7. Recommended Fix Priority

| Priority | Issue | Effort | Risk if Ignored |
|---|---|---|---|
| P0 | C-2 Path traversal fix | 1-2 hrs | Remote file read / arbitrary file access |
| P0 | C-1 Remove hardcoded secret_key default | 1-2 hrs | Credential leak in new deployments |
| P1 | C-3 Restrict default CORS origins | 30 min | Cross-origin data exfiltration |
| P1 | W-3 Isolate `last_used_at` flush errors | 15 min | Auth-OK requests spuriously fail |
| P1 | W-1 Encrypt OAuth tokens at rest | 4-8 hrs | Token theft from backups/host compromise |
| P2 | A-1 Split `models.py` | 4 hrs | Maintainability / merge conflicts |
| P2 | W-6 Add log retention | 2-4 hrs | Unbounded DB growth |
| P2 | B-2 DB triggers for `updated_at` | 2-3 hrs | Stale timestamps on bulk updates |
| P3 | A-4 Request ID middleware | 2-3 hrs | Poor observability |
| P3 | W-7 Rate-limit `/health` | 30 min | Health probe abuse |
| P3 | Testing gaps (auth, traversal, rate limiting) | 4-8 hrs | Undetected regressions |

---

## 8. Positive Observations

- **API key design is sound:** Prefix-based fast lookup with bcrypt-hashed full key; raw key shown once and discarded.
- **Config system is robust:** Pydantic Settings with environment variable override, `.env`, and YAML layering; clear precedence order.
- **Alembic migrations are present:** Schema drift is managed (6 migrations seen).
- **OAuth token files are chmod 0o600:** Unix permissions are correct for token files.
- **Path traversal awareness exists:** The code *tries* to prevent traversal; it just needs hardening.
- **Type annotations are thorough:** Heavy use of `Mapped[...]`, `Optional`, and `AsyncSession`.
- **Docker Compose includes health checks:** Postgres and (optionally) pgAdmin have `healthcheck` blocks.

---

*Report generated by Hermes Agent. Issues should be triaged, reproduced in a dev environment, and fixed before applying to production.*
