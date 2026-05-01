# Grimoire Code Review Report

**Date:** 2026-05-01  
**Reviewer:** Automated deep analysis  
**Scope:** Full codebase — core, search, API/DB, storage/CLI, tests/config, agents/docs

---

## Summary

| Severity | Count |
|----------|-------|
| **Critical** | 7 |
| **High** | 18 |
| **Medium** | 31 |
| **Low** | 25 |

The most impactful issues are: **asyncio thread-safety bugs in the watcher**, **path traversal in API endpoints**, **no authentication on any API route**, **event loop blocking in embedder/tagger**, and **FTS query construction errors**.

---

## Critical Issues

### C1. `asyncio.create_task()` Called From Wrong Thread (watch_manager.py:395)
The `_WatchdogEventHandler.on_any_event` runs on the watchdog observer thread but calls `asyncio.create_task()`, which requires the calling thread to own the event loop. This will raise `RuntimeError` at runtime, breaking all local file-watch callbacks.

**Fix:** Store a reference to the event loop at construction time and use `loop.call_soon_threadsafe()`:
```python
self._loop = asyncio.get_event_loop()
# In on_any_event:
self._loop.call_soon_threadsafe(
    lambda: asyncio.ensure_future(self._async_callback_wrapper(change), loop=self._loop)
)
```

### C2. MOVED Event Path/Previous-Path Swapped (watch_manager.py:385-389)
For moved events, `src_path` (old location) is assigned to `path` and `dest_path` (new location) to `previous_path`. This is semantically inverted — downstream consumers relying on `FileChange.path` for the new location will get the wrong path.

**Fix:** Swap `src_path` and `dest_path` in the `FileChange` construction to match `local.py`'s correct implementation.

### C3. No Authentication on Any API Endpoint (api/main.py)
The entire API has no authentication middleware, no API key requirement, and no authorization checks. Any network-accessible client can ingest arbitrary files, delete documents, start watchers, and generate content.

**Fix:** Add authentication middleware (API key header, JWT, or OAuth2). At minimum, add a FastAPI dependency that validates `settings.api.secret_key`.

### C4. Path Traversal in API Ingest Endpoints (api/schemas.py, api/routes/ingest.py)
`IngestFileRequest.file_path` and `IngestDirectoryRequest.directory` accept arbitrary strings with no path validation. An attacker can submit `../../etc/passwd` to read any file on the system.

**Fix:** Add a Pydantic field validator that resolves paths and rejects those outside an allowed base directory:
```python
@field_validator("file_path")
@classmethod
def validate_path(cls, v: str) -> str:
    resolved = Path(v).resolve()
    if not resolved.is_relative_to(ALLOWED_BASE):
        raise ValueError("Path traversal not allowed")
    return v
```

### C5. `allow_dangerous_deserialization=True` in rag_pipeline.py (lines 57, 72)
`FAISS.load_local(..., allow_dangerous_deserialization=True)` enables `pickle` deserialization, which can execute arbitrary code if the index file has been tampered with.

**Fix:** Ensure the vectorstore directory has strict permissions, or remove FAISS local persistence in favor of ChromaDB/Qdrant (already configured).

### C6. Embedder Blocks Event Loop (core/embedder.py:258-263, 346-352)
Both `embed_single()` and `embed()` are `async` methods that call `model.encode()` synchronously. This is CPU-intensive and blocks the entire event loop for seconds during embedding generation.

**Fix:** Wrap in `run_in_executor`:
```python
embeddings = await asyncio.get_running_loop().run_in_executor(
    None, lambda: self._model.encode(texts, **kwargs)
)
```

### C7. Tagger HTTP Client Closed After Every Call (core/tagger.py:578-579)
`suggest_tags()` closes the `httpx.AsyncClient` in a `finally` block after every call. This defeats connection reuse and creates a race condition when concurrent tasks share the tagger.

**Fix:** Make `Tagger` an async context manager (`__aenter__`/`__aexit__`) that manages the client lifecycle, or provide an explicit `close()` method. Remove the `finally: await self._close_client()`.

---

## High Issues

### H1. `__all__` Overwritten in core/__init__.py (line 12 vs 42)
The file assigns `__all__` twice — the second assignment completely overwrites the first, silently dropping parser exports (`DOCLEY_AVAILABLE`, `DocumentParser`, etc.). A `from grimoire.core import *` will miss these symbols.

**Fix:** Merge both `__all__` lists into a single definition.

### H2. Tagger Overwrites User-Assigned Tags (core/tagger.py:650-652)
When an LLM suggestion has higher confidence, `apply_tags` overwrites `existing.tagged_by` from `USER` to `LLM`. This silently replaces user-assigned tags.

**Fix:** Never change `tagged_by` from `USER` to `LLM`. Only update confidence when the existing tag was also LLM-assigned.

### H3. Blocking I/O in Async `check_file` (core/dedup.py:226)
`check_file` is `async` but calls `compute_file_hash(file_path)` which performs synchronous file I/O, blocking the event loop for large files.

**Fix:** `file_hash = await asyncio.get_running_loop().run_in_executor(None, compute_file_hash, file_path)`

### H4. FTS `escape_special_chars` Produces Invalid PostgreSQL tsquery (search/fulltext.py:90-97)
The function escapes FTS operators with backslash (e.g., `!` becomes `\!`), but PostgreSQL tsquery has no backslash escape syntax. This causes syntax errors or silent misinterpretation.

**Fix:** Strip or replace FTS operator characters instead of escaping them. Remove the `'` → `''` doubling (bind params handle SQL escaping).

### H5. Reranking Completely Replaces Hybrid Score (search/hybrid.py:376)
When reranking is enabled, `result.score = 1.0 - (rank / len(top_indices))` replaces the weighted vector+FTS score entirely, making `vector_weight` and `fts_weight` irrelevant.

**Fix:** Blend: `result.score = 0.6 * rerank_score + 0.4 * result.score`, or store the rerank score in a separate field.

### H6. Category CASCADE Delete Destroys Entire Subtree (db/models.py:450)
`categories.parent_id` uses `ondelete="CASCADE"`, combined with `cascade="all, delete-orphan"` on the `children` relationship. Deleting a parent category cascade-deletes ALL descendants.

**Fix:** Change to `ondelete="SET NULL"` and remove `cascade="all, delete-orphan"` from `children`.

### H7. `filter_dict` Accepts Arbitrary Unstructured Dicts (api/schemas.py:66,98)
`QueryRequest` and `SearchRequest` accept `filter_dict: dict[str, Any] | None` passed directly to ChromaDB's filter parser without validation.

**Fix:** Define a stricter schema for filter dictionaries (allow only known keys like `"tags"`, `"document_id"`) with a Pydantic model validator.

### H8. CORS Allows All Origins (api/main.py:39)
`allow_origins=["*"]` allows any website to make cross-origin requests to the API, compounding the lack of authentication.

**Fix:** Restrict to actual frontend domains via `settings.api.allowed_origins`.

### H9. Hardcoded Default Secret Key (config/settings.py:693)
`secret_key: str = Field(default="change-me-in-production")` — if deployed without overriding, JWTs can be forged by anyone who reads the source.

**Fix:** Remove the default and make it required, or add a startup check that refuses to run if the key is unchanged in production.

### H10. Migrate CLI Uses Nonexistent Config Attributes (cli/migrate.py:89,98-99)
`settings.CHROMADB_PATH`, `settings.QDRANT_URL`, and `settings.QDRANT_API_KEY` don't exist. The actual settings use nested attributes (`settings.vector_store.chromadb.path`, etc.). The migrate command will crash with `AttributeError`.

**Fix:** Use the correct nested attribute paths.

### H11. Path Traversal in Wiki Export (cli/wiki.py:167)
`page.slug` from the database is used directly as a filename: `export_dir / f"{page.slug}.md"`. A slug containing `../` could write files outside the export directory.

**Fix:** Validate the slug or use `resolve()` with a containment check.

### H12. Google Drive Query Injection (storage/gdrive.py:558-564)
`folder_id` extracted from user paths is interpolated directly into Drive API query strings using f-strings.

**Fix:** Escape single quotes in `folder_id` by replacing `'` with `\'`.

### H13. Config `show` Command Leaks Secrets (cli/config.py:96)
`settings.model_dump()` dumps ALL settings including database URLs with passwords and OAuth client secrets. `model_dump_redacted()` exists but is not used.

**Fix:** Use `settings.model_dump_redacted()`, or add a `--reveal` flag.

### H14. Cloud Poll Loop Double-Sleep on Error (watch_manager.py:206-229)
On error, the handler sleeps for `poll_interval`, then the loop immediately sleeps again at the top. This doubles the effective wait time after errors.

**Fix:** Remove the sleep from the error handler, or use a separate shorter backoff interval.

### H15. OneDrive Delta Token Injection (storage/onedrive.py:494)
The delta token is interpolated directly into a URL path: `f"/me/drive/root:/delta(token='{delta_token}')"`. Special characters in the token could break the URL.

**Fix:** URL-encode the delta token, or use the `@odata.deltaLink` URL directly.

### H16. Category Slug Generation Is Naive (api/routes/categories.py:54)
`slug = request.name.lower().replace(" ", "-")` doesn't handle special characters, consecutive spaces, Unicode, or slug collisions. Two categories can produce colliding slugs.

**Fix:** Use `python-slugify` and add a try/except around the insert to catch `IntegrityError` and return 409 Conflict.

### H17. `tags`, `tag_count`, `chunk_count` Never Populated (api/routes/documents.py:80-91)
`DocumentDetailResponse` always returns defaults (`tags=[]`, `tag_count=0`, `chunk_count=0`), never populating from the actual document data.

**Fix:** Populate these fields from `doc.tags` and `doc.chunks` with appropriate eager loading.

### H18. Ingest Endpoints Accept Arbitrary File Paths (api/routes/ingest.py)
See C4 above — same issue but noted here as a standalone security concern.

---

## Medium Issues

### M1. Missing Module Exports in core/__init__.py
`reranker.py`, `cache.py`, and `embedder.py` are not imported or re-exported. Users must import from submodules directly.

### M2. Pydantic Mutation in tagger._match_suggestions_to_categories (tagger.py:506)
`TagSuggestion.category_id = category_id` mutates input objects in place. Could break callers who reuse the suggestion list.

### M3. Reranker Returns Empty List Instead of Raising ValueError (reranker.py:92)
Docstring says it raises `ValueError` for empty input, but it returns `[]`.

### M4. Thread-Unsafe Lazy Model Init in Reranker (reranker.py:83-89)
Two concurrent calls could both see `_model is None` and create two `CrossEncoder` instances.

### M5. Timezone-Naive vs Timezone-Aware Datetime Comparison (dedup.py:302)
`_is_version_conflict` compares `existing_doc.updated_at` (timezone-aware) with `file_mtime` which may be naive, causing `TypeError` on Python 3.12+.

### M6. DiskCache Double-Parse Issue (cache.py:619-624)
`DiskCache.get` always tries `json.loads()`, but `DiskCache.set` conditionally serializes. Raw strings that happen to be valid JSON get double-parsed.

### M7. DiskCache.delete Swallows Errors (cache.py:661-673)
Catches all exceptions with only a warning, violating the ABC contract that says `delete` should raise `RuntimeError` on failure.

### M8. Empty Quoted Phrase Produces Invalid tsquery (fulltext.py:149,180-186)
The regex `r'\s*"([^"]*)"\s*'` matches empty quotes `""`, producing an empty tsquery that PostgreSQL rejects.

### M9. Missing GIN Index on Chunk.content tsvector (db/models.py)
Without a GIN index, every FTS query computes `to_tsvector` on every row, causing sequential scans.

**Fix:** Add a computed tsvector column with GIN index:
```python
content_tsvector = mapped_column(
    Computed("to_tsvector('english', content)", persisted=True)
)
```

### M10. Vector/FTS Search Failures Silently Swallowed (hybrid.py:251-253, 299-301)
Both `_vector_search` and `_fts_search` catch all exceptions and return empty lists. Infrastructure failures are invisible.

**Fix:** Log at WARNING level, or return a `degraded` flag.

### M11. `VectorSearch` Not Exported from search/__init__.py
Only `FulltextSearch` and `HybridSearch` are exported. `from grimoire.search import VectorSearch` fails.

### M12. `hasattr` Checks Couple to ChromaDB Implementation (hybrid.py:215,218)
`hasattr(self._vector_store, 'is_initialized')` and `getattr(self._vector_store, 'collection_name')` rely on ChromaDB-specific attributes not in the `VectorStore` ABC.

### M13. OneDrive 401 Token Refresh Logic Is Inverted (storage/onedrive.py:230-239)
The condition `not self.token_data.is_expired()` means "only refresh if token is NOT expired" — backwards. When a 401 is received, the token IS expired.

### M14. Google Drive `__del__` Uses Deprecated Event Loop Pattern (gdrive.py:896-901)
`asyncio.get_event_loop().create_task(self.close())` will raise `RuntimeError` in Python 3.12+ during garbage collection.

### M15. OAuth Token Files Briefly World-Readable (gdrive.py:191-194, onedrive.py:143-147)
Files are written with default permissions, then `chmod 0o600`. Between write and chmod, tokens are readable on multi-user systems.

### M16. OneDrive `list_files` Recursive Uses Delta API Incorrectly (onedrive.py:370-387)
Recursive listing uses the `/delta` endpoint, which returns all changes since the last token — not files in a specific folder.

### M17. Google Drive `list_files` Has No Depth Limit (gdrive.py:588-598)
Recursive calls have no depth limit. Deeply nested folders or circular shortcuts could cause excessive API calls.

### M18. LIKE Wildcard Injection (cli/docs.py:99)
`Document.title.ilike(f"%{search}%")` interprets `%` and `_` in user input as LIKE wildcards.

### M19. `watch unwatch` Creates New WatchManager (cli/watch.py:118-136)
The `unwatch` command creates a new `WatchManager` instance that has no knowledge of active watches. It will always report "watch not found."

### M20. `--force` Delete Leaves Orphaned DocumentTags (cli/categories.py:167-168)
Deleting a category with `--force` doesn't delete associated `DocumentTag` rows first, causing a foreign key violation if cascading deletes aren't configured.

### M21. `_parse_since` "m" Unit Is Ambiguous (cli/docs.py:20-42)
`3m` is interpreted as 30 days (months), but "m" commonly means "minutes" in other contexts.

### M22. Double Commit from Dependency + Route Handlers (api/routes/*.py)
The `get_db()` dependency auto-commits, then route handlers call `await db.commit()` again. Wastes a database round-trip and signals confusion about session lifecycle.

### M23. `GenerateRequest.content_type` Not Validated at Schema Level (api/schemas.py:129)
The field is `str` but the DB uses `ContentType` enum. Invalid strings only fail at runtime, not at request validation.

### M24. `WatchStartRequest.backend` Not Validated (api/schemas.py:225)
`backend: str = "local"` accepts any string instead of validating against the `StorageBackend` enum.

### M25. No Index on `DocumentTag.category_id` (db/models.py:500-508)
Composite PK `(document_id, category_id)` creates an index with `document_id` as leading column. Reverse lookups by `category_id` require sequential scans.

### M26. `WikiPage.target_refs` Missing Cascade Delete (db/models.py:919-923)
`source_refs` has `cascade="all, delete-orphan"` but `target_refs` doesn't. Orphaned cross-references can result.

### M27. `list_changes` Ignores `since` Parameter (gdrive.py:781, onedrive.py:485)
Both Google Drive and OneDrive `list_changes()` accept a `since: datetime` parameter but never use it for filtering.

### M28. `search_by_text` Uses Batch `embed()` Instead of `embed_single()` (vector.py:91)
`VectorSearch.search_by_text` calls `embedder.embed([query_text])` (batch), while `HybridSearch._vector_search` uses `embed_single(query)` (single). Inconsistent and slightly less efficient.

### M29. PIL Image Not JSON-Serializable (parser.py:423)
`img_data["pil_image"] = pic.image.to_pil()` stores a PIL Image object in the `ParsedDocument.images` list, which cannot be JSON-serialized.

### M30. ChromaDB Metadata Deserialization Corrupts Comma-Containing Strings (chromadb.py:446-448)
Strings containing commas are unconditionally split into lists, corrupting values like `"Washington, DC"`.

### M31. Path Parameters Not Validated as UUIDs (api/routes/*.py)
All `document_id`, `category_id`, `watch_id` path params are `str`. Invalid UUIDs produce 404 instead of 422.

---

## Low Issues

### L1. `apply_tags` Doesn't Check for Duplicate Tags (categories.py:198-213)
No uniqueness check before creating `DocumentTag`, allowing duplicates.

### L2. `_resolve_version_conflict` Is `async` With No `await` (dedup.py:304)
Unnecessary coroutine overhead. Remove `async` keyword.

### L3. `DiskCache.get` Cannot Distinguish Cache Miss from Stored `None` (cache.py:605-628)
Both return `None`.

### L4. `DiskCache` Missing `ttl()` and `exists()` Methods
Inconsistent with `RedisCache`. Not in the ABC but users may expect them.

### L5. Mock-Detection Code in Production Parser Path (parser.py:314-315, 322-323)
`hasattr(md_value, "_mock_name")` is test infrastructure leaked into production code.

### L6. TOCTOU Race in Local File Operations (local.py:343-356)
`Path.exists()` checks followed by operations have a time-of-check-to-time-of-use window.

### L7. Naive vs Timezone-Aware Datetime Mixing (local.py, watch_manager.py)
`datetime.now()` (naive) mixed with `datetime.now(tz=timezone.utc)` (aware).

### L8. Google Drive `_parse_gdrive_path` Heuristic Is Fragile (gdrive.py:383-403)
`len(folder_id) > 20 or " " not in folder_id` misclassifies short IDs or long names.

### L9. OneDrive `_parse_odt_datetime` Returns `datetime.now()` for Missing Dates (onedrive.py:261-268)
Fabricates timestamps instead of returning `None`.

### L10. Silent ImportError in storage/__init__.py (lines 20-29)
`GoogleDriveAdapter` import failure is silently caught but the class is still in `__all__`, causing `AttributeError`.

### L11. Migrate Command Is a Stub (cli/migrate.py:117-140)
The command increments counters but doesn't actually migrate data. No warning to users.

### L12. Thread-Unsafe Lazy Converter Init in Parser (parser.py:196-222)
Two concurrent `_parse_sync` calls could both initialize the `DocumentConverter`.

### L13. Thread-Unsafe Lazy Model Loading in Embedder (embedder.py:124-162)
Same race condition as parser and reranker — `_load_model` needs `threading.Lock` or `asyncio.Lock`.

### L14. Unvalidated Device String in Embedder (embedder.py:104)
Strings like `"tpu"` or `"cudo"` pass through silently and fail deep in PyTorch.

### L15. `EmbedderFactory.create` Misleading Docstring (embedder.py:466-483)
Says "Creates DiskCache if None" but actually requires `cache_path` to be provided.

### L16. `rerank_top_k or top_k` Treats Explicit 0 as Falsy (hybrid.py:131)
Should use `top_k if rerank_top_k is None else rerank_top_k`.

### L17. Redundant Empty-Check in `_fts_search` (hybrid.py:279)
Method returns early on empty results, making the `if fts_results` guard redundant.

### L18. No Upper-Bound Validation on `vector_top_k` and `fts_top_k` (hybrid.py:107-108)
Can cause enormous database/vector store loads with no limit.

### L19. `parse_query` Doesn't Handle Unbalanced Quotes (fulltext.py:123,149)
`"hello world` (one opening quote) passes through with the quote character intact.

### L20. Duplicate Query-Building Logic (fulltext.py:257-356 vs 358-415)
`search_chunks_only` copies most of `_execute_search` with `include_title_weight=False`.

### L21. Double Commit from Dependency + Routes (api/routes/*.py)
Already covered in M22.

### L22. `IngestFileRequest.storage_backend` Accepted But Never Used (api/routes/ingest.py:26,37-38)
The field is defined in the schema but never passed to the agent.

### L23. Wiki Models Missing from db/__init__.py Exports
`WikiPage`, `WikiPageSection`, `WikiCrossReference`, `WikiCompileJob` are not exported.

### L24. `color` Field Accepts Any String (api/schemas.py:195)
No validation that it's a valid hex color code. Will truncate to 7 chars in the DB.

### L25. Module-Level `app = create_app()` (api/main.py:59)
Creates the app at import time, before the lifespan context has run. Database-dependent code will fail.

---

## Documentation vs Implementation Gaps

### D1. DESIGN.md Describes LangChain Deep Agents — Not Implemented
The design doc (Section 5) describes agents as "LangChain Deep Agents" with "prebuilt tools, automatic context compression, subagent spawning." The actual implementation uses simple classes with `httpx` calls to the Ollama API. No LangChain dependency exists.

### D2. DESIGN.md Describes `relationships` Table — Not Implemented
The design doc defines a `relationships` table (source/target document links with `relationship_type` and `confidence`), but this table and model don't exist in `db/models.py`.

### D3. DESIGN.md Describes `processing_log` Table — Not Implemented
The `processing_log` table exists as `ProcessingLog` in models.py but is only partially used. The `ActionType` enum doesn't include all the actions described (`discovered` is missing).

### D4. DESIGN.md Describes `cache_entries` Table — Not Implemented
No `CacheEntry` model exists. Caching uses Redis/DiskCache directly, not a database table.

### D5. DESIGN.md Describes Semantic Chunking Strategy — Not Used
The design doc specifies `SemanticChunker` as the default strategy. The actual `_select_chunking_strategy()` in `ingestion.py` only uses `MARKDOWN` for `.md` files and `RECURSIVE` for everything else. Semantic chunking is never selected.

### D6. DESIGN.md Describes Rate Limiting — Not Implemented
The `rate_limit.py` utility exists but is not wired into any cloud storage adapter or LLM call.

### D7. DESIGN.md Describes Observability/Tracing — Not Implemented
No OpenTelemetry or LangSmith integration exists. Only `loguru` logging is implemented.

### D8. DESIGN.md Describes Celery Task Queue — Not Implemented
No Celery integration exists. All processing is synchronous within asyncio.

### D9. DESIGN.md Describes QdrantStore — Not Implemented
The design doc describes `QdrantStore` alongside `ChromaDBStore`, but only `ChromaDBStore` is implemented. The `migrate --to qdrant` CLI command exists but is a stub.

### D10. DESIGN.md Describes `repositories/` Pattern — Not Implemented
The design doc shows `db/repositories/documents.py` and `db/repositories/categories.py`, but only `__init__.py` exists in the repositories directory.

### D11. DESIGN.md Describes `base.py` in Agents — Not Implemented
`agents/base.py` exists but contains only a docstring reference to shared agent utilities. No shared base class is implemented.

### D12. README Lists `grimoire reindex` Command — Not Implemented
The README mentions `grimoire reindex 42` but no `reindex` CLI command exists.

### D13. README Lists `grimoire cache stats` and `grimoire cache clear` — Partially Implemented
The `status` command shows some stats, but there's no dedicated `cache` CLI group.

### D14. WikiAgent Not in DESIGN.md Agent List
The `WikiAgent` and `CoordinatorAgent` exist in code but aren't described in the DESIGN.md agent section.

### D15. DESIGN.md Project Structure Doesn't Match Reality
Several directories/files described in Section 11 differ:
- `core/chunker/` is a subdirectory (matches), but `agents/base.py` is empty
- `db/repositories/` only has `__init__.py`
- No `CHANGELOG.md` file exists

---

## Test Coverage Gaps

### Missing Test Files (source modules with no corresponding test):
| Module | Test File | Status |
|--------|-----------|--------|
| `grimoire/core/reranker.py` | `test_reranker.py` | **Missing** |
| `grimoire/utils/path.py` | — | **Missing** |
| `grimoire/utils/rate_limit.py` | — | **Missing** |
| `grimoire/utils/observability.py` | — | **Missing** |
| `grimoire/utils/hash.py` | — | **Missing** |
| `grimoire/agents/coordinator.py` | `test_coordinator_agent.py` | Exists |
| `grimoire/agents/base.py` | — | **Missing** |
| `grimoire/api/routes/*` | `test_api.py` | Exists (partial) |
| `grimoire/db/session.py` | — | **Missing** |

### Critical Test Scenarios Not Covered:
1. **Concurrent access** — No test for the tagger HTTP client race condition (C7)
2. **Thread safety** — No test for parser/embedder/reranker lazy init race conditions
3. **Path traversal** — No test for malicious slugs in wiki export or ingest paths
4. **FTS edge cases** — No test for empty quoted phrases or special characters in queries
5. **Cloud storage auth** — GDrive/OneDrive tests mock everything, no real auth flow tests
6. **Data integrity** — No test for category cascade delete behavior
7. **Session management** — No test for the double-commit issue in API routes

---

## Recommended Priority Order

1. **C1-C7** — Critical security and correctness bugs. Fix immediately.
2. **H4, H5, H9** — FTS query errors, score calculation, CORS. High user impact.
3. **H6, H10-H18** — Data integrity, CLI crashes, security. Fix before next release.
4. **M8-M9, M22** — Missing index, double commit. Performance and correctness.
5. **Remaining Medium** — Address in upcoming sprints.
6. **Low** — Address opportunistically during related work.

---

*End of report.*