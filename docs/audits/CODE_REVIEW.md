# Grimoire Code Review Report

**Date:** 2026-04-11  
**Scope:** Full codebase — 57 source files, 25 test files  
**Analysis method:** 5 parallel specialist agents covering core pipeline, agents layer, infrastructure, API/CLI, and search/cross-cutting

---

## Executive Summary

| Category | Critical | Important | Minor |
|----------|----------|-----------|-------|
| Logic errors | 14 | 28 | 12 |
| Missing/incomplete implementation | 8 | 15 | 7 |
| Test coverage gaps | — | 67 | 19 |
| Documentation issues | — | 12 | 23 |

**Three systemic issues** appear across multiple independent modules and warrant immediate attention:

1. **LLM error strings persisted to DB/cache** — `query.py`, `content_gen.py` both cache degraded-answer strings and write them to the database with long TTLs, making transient failures permanent until TTL expiry or manual DB cleanup.
2. **`datetime.utcnow()` used throughout** — deprecated since Python 3.12 and produces naive datetimes that conflict with timezone-aware DB columns (`DateTime(timezone=True)`), causing `TypeError` at runtime when comparing timestamps.
3. **Two core features are entirely unimplemented:** the reranker (abstract base only, no concrete class) and cloud watch polling (the loop runs but never calls the adapter or user callback).

---

## Table of Contents

1. [Core Processing Pipeline](#1-core-processing-pipeline)
2. [Agents Layer](#2-agents-layer)
3. [Infrastructure Layer](#3-infrastructure-layer)
4. [API & CLI Layer](#4-api--cli-layer)
5. [Search Layer](#5-search-layer)
6. [Cross-Cutting Concerns](#6-cross-cutting-concerns)
7. [Documentation Issues](#7-documentation-issues)
8. [Global TODO/FIXME Inventory](#8-global-todofixme-inventory)
9. [Priority Fix List](#9-priority-fix-list)

---

## 1. Core Processing Pipeline

### 1.1 `grimoire/core/parser.py`

#### Logic Errors

**Double file-hash computation** (`parser.py:444, 513`)  
`parse()` computes the file hash at line 444 via `_compute_file_hash()`, then `_parse_sync()` computes it again at line 513. The first computation is always wasted — the result from `_parse_sync` always takes precedence.  
*Fix:* Remove the first hash computation from `parse()` and use only the result from `_parse_sync`.

**`asyncio.get_event_loop()` deprecation** (`parser.py:453`)  
`asyncio.get_event_loop().run_in_executor(...)` is deprecated since Python 3.10. Inside a running async context, `get_event_loop()` may return a closed or deprecated loop.  
*Fix:* Replace with `asyncio.get_running_loop().run_in_executor(...)`.

**Exception swallowed in `_process_docling_result`** (`parser.py:239`)  
When `export_to_markdown()` raises `AttributeError` or `TypeError`, the code silently falls through to `str(result.document)`. The inner fallback returns `status="success"` with potentially garbled text while the outer `except` at line 315 would set `status="failed"`. The two exception handlers work against each other.  
*Fix:* Log the inner exception and surface it, or explicitly handle the fallback case with a separate status flag.

**Mock-detection in production code** (`parser.py:247, 255`)  
`hasattr(md_value, '_mock_name')` and `hasattr(result, '_mock_name')` are test scaffolding leaking into production. Any real Docling object with a `_mock_name` attribute would have its text silently dropped.  
*Fix:* Remove these checks entirely; structure tests to not require this workaround.

**`_get_converter()` is not thread-safe** (`parser.py:150–152`)  
`_get_converter()` lazily creates `self._converter` without any lock. Two concurrent `parse()` calls race to initialize the converter. The test `test_concurrent_parses` exists but does not expose this race because the mock does not slow down enough.  
*Fix:* Use `asyncio.Lock` to guard initialization: `if not self._converter: async with self._lock: if not self._converter: ...`

#### Missing / Incomplete Implementation

**`enable_tables`, `enable_figures`, `ocr_enabled` are ignored** (`parser.py:94–97`)  
`ParserConfig` declares these fields but none are forwarded to the `DocumentConverter`. The converter is always constructed with defaults (`DocumentConverter()`). Callers who set these flags see no behavioral change.  
*Fix:* Forward these options to the Docling pipeline configuration at converter construction time.

**Image extraction returns non-serializable objects** (`parser.py:351–357`)  
`_extract_images` stores live PIL `Image` objects in `ParsedDocument.images`. PIL objects are not JSON-serializable, breaking any downstream serialization. The `Pydantic dict[str, Any]` field type permits this silently.  
*Fix:* Encode images as base64 strings before storing: `base64.b64encode(img_bytes).decode()`.

#### Test Coverage Gaps

- `parse()` with a directory path instead of a file (raises `IsADirectoryError` rather than returning `status="failed"`)
- `_detect_file_type()` on a file with no extension (produces empty string in error message)
- `ConversionStatus.PARTIAL_SUCCESS` result (untested status mapping)
- File hash failure path (empty-string hash propagates silently to result metadata)
- `custom_config` parameter override in `parse()`

#### Documentation Issues

**Misleading `parse()` docstring** (`parser.py:386–387`)  
States it raises `FileNotFoundError` and `ValueError`, but the implementation catches these and returns `ParsedDocument(status="failed")` instead.

**`DOCLEY_AVAILABLE` typo** (`parser.py:25`)  
Should be `DOCLING_AVAILABLE`. Creates a confusing inconsistency with usage elsewhere.

---

### 1.2 `grimoire/core/chunker/`

#### Logic Errors

**`markdown.py`: Header detection is non-deterministic** (`chunker/markdown.py:144–164`)  
`_split_text_by_headers` iterates over `set(self.config.headers_to_split_on)`, which has no defined iteration order. A `## Section` header can be misclassified as `#` because `"## Section".startswith("# ")` is `True` and the set may iterate `#` before `##`. `_build_header_pattern` sorts by length (correct) but `_split_text_by_headers` bypasses that regex entirely.  
*Fix:* Sort header prefixes by length descending before iteration, or use the `_build_header_pattern` regex for both detection and splitting.

**`markdown.py`: Overlap double-counts content** (`chunker/markdown.py:350–359`)  
When creating overlap, `overlap_text` is appended to `current_content` while it was already part of the previous chunk. The overlap is not bounded by `config.chunk_overlap`; it is always the last two paragraphs regardless of size.  
*Fix:* Compute `overlap_chars` from the actual character count and trim accordingly.

**`recursive.py`: `_recursive_split` reorders content** (`chunker/recursive.py:239–257`)  
The method partitions splits into `good_splits` (fits) and `large_splits` (too big), processes all `good_splits` first, then all `large_splits`. A document with alternating small/large paragraphs will have its content reordered in the output.  
*Fix:* Maintain insertion order by processing each split in sequence and recursing when needed inline.

**`recursive.py`: Regex separator reuse bug** (`chunker/recursive.py:145`)  
When `is_separator_regex=True` and `keep_separator=True`, `re.search(separator, text)` always searches the original full text, not the current part. Every part gets the same separator appended (the first match in the original string).  
*Fix:* Search for the separator in the current `part`, not in the original `text`.

**`semantic.py`: Stale chunk metadata after merge** (`chunker/semantic.py:218–224`)  
When small chunks are merged back into the previous chunk, `prev_chunk.content` is updated in place but `prev_chunk.metadata["end_sentence"]` and `prev_chunk.metadata["sentence_count"]` are not updated, leaving stale metadata.  
*Fix:* Update all affected metadata fields after merging.

**`semantic.py`: `_compute_embeddings` silently discards non-ndarray output** (`chunker/semantic.py:138`)  
If `model.encode()` returns a `torch.Tensor` instead of `ndarray` (e.g., when `convert_to_tensor=True` is active), the `isinstance` check silently drops the result and falls back to `_fallback_boundaries` with no indication that semantic chunking was not performed.  
*Fix:* Add `convert_to_numpy=True` to the `model.encode()` call, or handle `torch.Tensor` explicitly.

**`semantic.py`: `_fallback_boundaries` may omit the last sentences** (`chunker/semantic.py:256–265`)  
If the last sentence group does not trigger an `append` call (because `current_size < min_chunk_size`), those sentences are silently omitted from the output.  
*Fix:* After the loop, ensure a final boundary is added if there are remaining sentences.

**`base.py`: `_count_tokens` over-broad exception** (`chunker/base.py:192`)  
`except Exception` swallows encoding errors and ImportErrors without logging, and returns `max(0, len(text) // 4)` which yields `0` for very short strings.  
*Fix:* Narrow the exception to `(ImportError, AttributeError)`, log the failure, and return `max(1, len(text) // 4)`.

#### Missing / Incomplete Implementation

**`semantic.py`: Synchronous ML model in async method** (`chunker/semantic.py`)  
`_get_embedding_model()` and `_compute_embeddings()` load and run an ML model synchronously inside `async def chunk()`, blocking the event loop during model loading and inference.  
*Fix:* Wrap with `asyncio.get_running_loop().run_in_executor(None, ...)`.

**`semantic.py`: Hardcoded embedding model** (`chunker/semantic.py:102`)  
`_get_embedding_model` always loads `"all-MiniLM-L6-v2"`, ignoring any model configured in `ChunkConfig.encoding_name` or external settings.

**`recursive.py`: `for_code()` missing many languages** (`chunker/recursive.py:67`)  
Only handles Python, JS, TypeScript, Java, and Rust. Go, C, C++, Ruby, Kotlin, Swift, Bash silently fall back to generic separators with no warning.

#### Test Coverage Gaps

- `markdown.py`: `##` header correctly distinguished from `#` (exposes ordering bug)
- `markdown.py`: Single paragraph exceeding `chunk_size` (not further split)
- `markdown.py`: `keep_headers=False` behavior
- `recursive.py`: `is_separator_regex=True` mode (the entire regex path is untested)
- `recursive.py`: Content ordering after `_recursive_split` on alternating segment sizes
- `semantic.py`: Behavior when `sentence-transformers` is not installed
- `semantic.py`: Chunk metadata consistency after small-chunk merging
- `semantic.py`: Text with no sentence-ending punctuation (single-sentence result)
- `base.py`: `_count_tokens` fallback when tiktoken unavailable
- `base.py`: `_set_continuity_links` with a single-element list

---

### 1.3 `grimoire/core/embedder.py`

#### Logic Errors

**`_get_device()` with explicit `"mps"` crashes if torch is absent** (`embedder.py:88–104`)  
The auto-detect branch has an `except ImportError` guard, but the explicit-device branch does not. Requesting `device="mps"` without torch installed raises `ImportError` instead of gracefully returning `"cpu"`.  
*Fix:* Wrap the explicit-device MPS check in `try/except ImportError` and fall back to `"cpu"`.

**Dead error-handling code** (`embedder.py:370–372`)  
`assert None not in results` followed by `raise RuntimeError` can never trigger in practice (indices are guaranteed by construction). This provides false safety assurance.

#### Missing / Incomplete Implementation

**No `close()` or context manager** — The embedder holds a loaded ML model in memory indefinitely with no mechanism for callers to release GPU memory or unload the model. Long-running services accumulate loaded models as `Embedder` instances are created and discarded.

#### Test Coverage Gaps

- `_get_device()` with `device="mps"` when torch is not installed
- `embed()` with a list containing `None` values
- Cache backend failure during `embed()` batch (verify cache failure does not corrupt returned embeddings)
- Model fallback path (`_load_model` fallback to `config.fallback_model`)
- `EmbeddingConfig` with `normalize_embeddings=False`

---

### 1.4 `grimoire/core/tagger.py`

#### Logic Errors

**Unbounded recursion on circular category hierarchy** (`tagger.py:256`)  
The inner `build_path` function recurses via `parent = categories_by_id.get(cat.parent_id)`. A circular reference (A→B→A) causes `RecursionError`. No cycle detection exists.  
*Fix:* Track visited IDs in a set and break the loop when a cycle is detected.

**Greedy regex captures multiple JSON objects** (`tagger.py:363`)  
`re.search(r"\{.*\}", response_text, re.DOTALL)` matches from the first `{` to the last `}`. If the LLM response contains multiple JSON objects, the captured string is malformed and `json.loads` fails, silently returning `[]`.  
*Fix:* Use a non-greedy match (`r"\{.*?\}"`) or a proper JSON parser that handles streaming extraction.

**Category name collision (first-wins, no warning)** (`tagger.py:479–483`)  
`name_to_id` is built with first-wins policy. Two categories with the same `name` in different subtrees cause the second to be silently discarded, potentially mapping LLM suggestions to the wrong category.  
*Fix:* Use the full slug/path as the lookup key, not just the name.

**HTTP client is never closed** (`tagger.py:213`)  
`_get_client()` creates an `httpx.AsyncClient` stored in `self._client`. `_close_client()` exists but is never called automatically. Each `Tagger` instance leaks an open HTTP connection pool.  
*Fix:* Implement `__aenter__`/`__aexit__` or call `_close_client()` in a `finally` block inside `suggest_tags`.

**`TaggingResult.cached` is always `False`** (`tagger.py:146`)  
The field is documented as indicating a cached result, but no caching layer is implemented. The field is a placeholder that permanently misleads callers using it for metrics.

#### Missing / Incomplete Implementation

**`tag_document` with no content raises unhelpful `ValueError`** (`tagger.py:701`)  
If the document has no chunks and no title, `sample` becomes `""`, and `suggest_tags` raises `ValueError("Document sample cannot be empty")` with no message indicating the document has no content.  
*Fix:* Check for empty sample before calling `suggest_tags` and return a `TaggingResult` with a descriptive error message.

#### Test Coverage Gaps

- Circular category hierarchy (`RecursionError` scenario)
- Two categories with the same name in different subtrees
- `_close_client()` lifecycle / HTTP client cleanup
- `tag_document` with empty chunks and no title
- LLM response with multiple JSON objects
- `_call_ollama` with response missing `"response"` key
- `apply_tags` lower-confidence path (same or lower confidence should not overwrite existing tag)

---

### 1.5 `grimoire/core/dedup.py`

#### Logic Errors

**Timezone-aware vs naive datetime comparison** (`dedup.py:486, 303`)  
`datetime.fromtimestamp(mtime)` returns a naive datetime. `Document.updated_at` is stored in a `DateTime(timezone=True)` column and is timezone-aware. Comparing them in `_is_version_conflict` raises `TypeError: can't compare offset-naive and offset-aware datetimes` at runtime.  
*Fix:* Replace `datetime.fromtimestamp(mtime)` with `datetime.fromtimestamp(mtime, tz=timezone.utc)`.

**`AUTO` strategy is functionally identical to `SKIP`** (`dedup.py:326–333`)  
When a version conflict is detected, both `AUTO` and `SKIP` keep the existing document. The comment acknowledges the confusion. `AUTO` should update when the incoming file is newer — but this case is already handled by the non-conflict `UPDATE` path. The conflict-path behavior of `AUTO` is a semantic dead-end.  
*Fix:* Document explicitly that `AUTO` in a conflict means "keep existing." Consider renaming to clarify intent.

**`SKIP` mapped to `StatusType.PARTIAL`** (`dedup.py:409`)  
Skipping a duplicate is a successful outcome, not a partial one. Monitoring dashboards will misclassify successful deduplication as incomplete processing.  
*Fix:* Map `SKIP` to `StatusType.SUCCESS`.

**Synchronous hash computation blocks event loop** (`dedup.py:227`)  
`compute_file_hash` is called synchronously inside `async check_file`. For large files this blocks the event loop.  
*Fix:* Wrap in `asyncio.get_running_loop().run_in_executor(None, compute_file_hash, file_path)`.

#### Test Coverage Gaps

- Timezone-aware vs naive datetime comparison (the `TypeError` path)
- `AUTO` vs `SKIP` producing identical behavior in conflict case
- `compute_file_hash` on binary files with null bytes
- `strategy` parameter override in `check_file`
- Symlink handling (broken symlink raises `FileNotFoundError`)

#### Documentation Issues

**Doctest example is wrong** (`dedup.py:121`)  
`print(hash_value)` in the doctest would not output surrounding quotes, but the example shows `'a3f7c2d8...'` with quotes. The doctest would fail if run.

---

### 1.6 `grimoire/core/reranker.py`

#### Logic Errors — **BLOCKING**

**No concrete implementation exists**  
`reranker.py` contains only the abstract `Reranker` ABC. The `CrossEncoderReranker` referenced in the docstring and in the agents layer does not exist anywhere in the codebase. The reranking step in the retrieval pipeline is entirely unimplemented — the pipeline references it but cannot use it.  
*Fix:* Implement `CrossEncoderReranker` using `sentence-transformers` cross-encoders (e.g., `cross-encoder/ms-marco-MiniLM-L-6-v2`).

**`rerank()` contract underspecified**  
No guidance on behavior when `top_k <= 0` or `documents` is empty. Concrete implementations must handle these independently with no base-class guidance.

#### Test Coverage Gaps

- No `test_reranker.py` exists — zero coverage
- `rerank()` with `top_k > len(documents)`
- `rerank()` with `top_k=0`
- `rerank()` with empty `documents` list

---

## 2. Agents Layer

### 2.1 `grimoire/agents/ingestion.py`

#### Logic Errors

**Stale SQLAlchemy ORM object on UPDATE** (`ingestion.py:297–301`)  
On `DeduplicationAction.UPDATE`, `dedup_result.existing_document` is used directly without refresh. In async SQLAlchemy, accessing lazy-loaded relationships (`doc.chunks`) on a potentially expired object raises `MissingGreenlet` or `DetachedInstanceError`.  
*Fix:* `await db.refresh(doc, ["chunks"])` before calling `_update_document_record`.

**No transaction rollback on partial failure** (`ingestion.py:356–363`)  
The outer `except` block returns `IngestionResult(status="failed")` without calling `db.rollback()`. If `_create_document_record` succeeded but a later step failed (e.g., `_store_chunks_in_db` partially wrote), orphaned chunk rows remain and the document is stuck in `PROCESSING` status permanently.  
*Fix:* Add `await db.rollback()` in the `except` block.

**`asyncio.CancelledError` swallowed** (`ingestion.py:356`)  
The broad `except Exception` catches `asyncio.CancelledError` in older Python versions. Even in 3.8+ where it no longer inherits from `Exception`, the habit of catching `Exception` broadly in async code is dangerous.  
*Fix:* Add an explicit `except asyncio.CancelledError: raise` before the broad `except`.

**`datetime.utcnow()` deprecated** (`ingestion.py:335, 719`)  
Produces naive datetimes that conflict with `DateTime(timezone=True)` columns. Same issue throughout the codebase.  
*Fix:* Replace with `datetime.now(tz=timezone.utc)`.

#### Missing / Incomplete Implementation

**`_log_extraction` is permanently a no-op** (`ingestion.py:752–762`)  
The method exists, is called, but contains only `pass`. Every successfully parsed document is missing its extraction step in the audit trail. The docstring says "Log successful extraction" — this is misleading.  
*Fix:* Either implement extraction logging after document creation, or remove the method and its call-site.

#### Test Coverage Gaps

- `ingest_file` with `file_path=None` (produces confusing `TypeError` via broad `except`)
- **UPDATE re-ingest path** — `_update_document_record` (delete old vectors, delete old chunks, bump version) is completely untested
- `add_documents` raises after embedder succeeds (DB chunks written but no vectors)
- `ingest_directory` with `recursive=False`
- Concurrent `ingest_file` calls on the same file (race to create duplicate document rows)

---

### 2.2 `grimoire/agents/query.py`

#### Logic Errors

**LLM error strings are cached** (`query.py:220–223, 410–415`)  
When `_generate_answer` fails (network error, JSON decode error, timeout), it returns a fallback error string. The outer `query()` method caches this string in Redis with a 1-hour TTL. Subsequent identical queries within the TTL window receive the cached error as their answer — making a transient failure permanent until TTL expiry.  
*Fix:* Check whether the answer is an error sentinel before caching, or use a typed `QueryResult.llm_error: bool` field.

**`get_document_details` lazy-loads relationships** (`query.py:296–303`)  
`doc.chunks` and `doc.tags` are accessed after a simple scalar query with no `selectinload`. In async SQLAlchemy this raises `MissingGreenlet`.  
*Fix:* Add `options(selectinload(Document.chunks), selectinload(Document.tags))` to the query at line 282.

**`search()` has no empty-query guard** (`query.py:227–268`)  
`query()` guards against empty/None queries but `search()` does not. An empty string is passed directly to `_hybrid_search.search()`.

#### Missing / Incomplete Implementation

**No timeout distinction in results**  
`httpx.ReadTimeout` after 2 minutes is caught by broad `except Exception` and cached as a degraded answer. Callers cannot distinguish a timeout from a brief but valid answer. A `QueryResult.llm_error: bool` or `llm_timed_out: bool` field is needed.

**`search()` is undocumented as cache-free**  
The class docstring says "with caching" but `search()` never checks or stores in cache. The asymmetry is undocumented.

#### Test Coverage Gaps

- `_generate_answer` with malformed JSON from LLM (the poisoned-cache scenario)
- LLM returns HTTP 5xx (`raise_for_status` raises, result cached)
- `httpx.ReadTimeout` path
- `get_document_details` with a valid and invalid document ID (entire method untested)
- `search()` with an empty query
- Concurrent identical queries racing for cache write

---

### 2.3 `grimoire/agents/content_gen.py`

#### Logic Errors

**LLM error strings written to database** (`content_gen.py:340–350, 443–448`)  
`_call_llm` catches all exceptions and returns an error sentinel string (e.g., `"Error: LLM service unavailable..."`). This string is then written to `GeneratedContent` in the database with no error flag. On subsequent requests, `_check_existing` returns this error string as `cached=True`. A transient Ollama restart permanently contaminates the DB for that document/content-type pair until manually cleaned up. A **30-day cache TTL** compounds this.  
*Fix:* Add a `GeneratedContent.is_error: bool` column, or check for error sentinel before persisting. Never store error strings as content.

**`_PROMPTS` silent fallback on unknown content type** (`content_gen.py:407`)  
`_PROMPTS.get(request.content_type, _PROMPTS[ContentType.SUMMARY])` silently uses the SUMMARY template for any unrecognized `ContentType`. Adding a new enum value without a prompt entry produces confusing output with no warning.  
*Fix:* Raise `KeyError` or log a `WARNING` when falling back to the default prompt.

**`_call_llm` returns `""` when `"response"` key absent** (`content_gen.py`)  
`data.get("response", "").strip()` returns an empty string for a valid but unexpected API response. This empty string is stored as content and returned as a successful generation.  
*Fix:* Raise an explicit error when the `"response"` key is missing.

#### Test Coverage Gaps

- LLM error stored in DB then returned as `cached=True` on next request (the poisoned-DB scenario)
- `generate()` with `document_ids=[]`
- `_call_llm` response missing `"response"` key
- Content truncation in `_fetch_document_content` end-to-end (only `_build_prompt` is tested)

---

### 2.4 `grimoire/agents/coordinator.py`

#### Logic Errors

**Substring keyword matching causes false positives** (`coordinator.py:140–144`)  
`if kw in lowered` matches short keywords embedded in unrelated words. "parse" (INGEST keyword) matches "I want to parse a query about...", which should be a QUERY intent. No whole-word matching or word boundary check is applied.  
*Fix:* Use `re.search(r'\b' + re.escape(kw) + r'\b', lowered)` for word-boundary matching.

**`_llm_classify` substring match ambiguity** (`coordinator.py:736–739`)  
`if intent.value in raw` matches `"ingest"` inside `"ingestion"` if the LLM responds with that word. After normalizing `raw`, the check should use `== raw`, not `in raw`.

**`_handle_watch` passes raw string backend** (`coordinator.py:667–670`)  
`ctx.watch_backend` (a plain `str`) is passed to `WatcherAgent.watch(backend=...)`. An invalid backend string causes `StorageBackend(backend)` inside `WatcherAgent` to raise an unhandled `ValueError`.  
*Fix:* Validate or convert `ctx.watch_backend` to `StorageBackend` in the coordinator before dispatch.

#### Missing / Incomplete Implementation

**`ingest` convenience method silently drops `auto_tag`** (`coordinator.py:405–429`)  
The `ingest()` method accepts `auto_tag` but the `CoordinatorContext` it constructs does not carry this field. `_handle_ingest` never passes `auto_tag` to the ingestion agent. The parameter is accepted and silently ignored.  
*Fix:* Add `auto_tag` to `CoordinatorContext` and thread it through to `_handle_ingest`.

#### Test Coverage Gaps

- `ingest()` with `auto_tag=False` is honoured (would catch the dropped parameter)
- `_handle_watch` with invalid backend string
- LLM returns `"ingestion"` (substring ambiguity in `_llm_classify`)
- `_extract_path` with a relative path (returns `None` — undocumented)
- Concurrent `execute()` calls

---

### 2.5 `grimoire/agents/watcher.py`

#### Logic Errors

**`get_status()` always reports `is_running=True`** (`watcher.py:243`)  
`WatchStatus(is_running=True, ...)` is hardcoded. If the `WatchManager` stopped a watch due to an error without calling `unwatch()`, the agent still reports it as running.  
*Fix:* Query `WatchManager` for actual live state per watch ID.

**Dead processor task leaves agent in zombie state** (`watcher.py:350–372`)  
`_process_events` catches non-`CancelledError` exceptions and exits. After this, `self._running` remains `True`, so future `watch()` calls skip `_start_processor()`. New events are placed in the queue but never processed. The agent appears alive but is stuck.  
*Fix:* Set `self._running = False` on unexpected exit from `_process_events`, and restart the processor on the next `watch()` call.

**`asyncio.QueueFull` handler is dead code** (`watcher.py:284–287`)  
`asyncio.Queue()` with no `maxsize` is unbounded and never raises `QueueFull`. The `except asyncio.QueueFull` block at line 285 is unreachable.  
*Fix:* Create the queue with `asyncio.Queue(maxsize=N)` if backpressure is intended, or remove the dead handler.

**MOVED events are silently dropped** (`watcher.py:301–305`)  
`_should_process` returns `False` for `FileChangeType.MOVED`. A file moved into the watched directory from outside triggers no ingestion, which is almost certainly unintended.

#### Missing / Incomplete Implementation

**No watch persistence** — When the process restarts, all watches are lost. `WatchPath` is imported but never read from or written to the database. The "long-running daemon" documentation implies persistent watches.

#### Documentation Issues

**`WatcherAgent` docstring example** (`watcher.py:110`)  
Shows `await watcher.stop(watch_id)` — the method is named `unwatch`, not `stop`. This example calls a non-existent method.

---

## 3. Infrastructure Layer

### 3.1 `grimoire/db/models.py`

#### Logic Errors

**`datetime.utcnow` as column default throughout** (`models.py:219, 225, 358, 429, 483, 550, 609, 695, 750, 815`)  
Every `created_at`/`updated_at` column uses `default=datetime.utcnow` producing naive datetimes, but the column type is `DateTime(timezone=True)`. This causes silent timezone misinterpretation or `TypeError` at comparison time.  
*Fix:* Use `server_default=func.now()` for DB-side timestamps, or `default=lambda: datetime.now(timezone.utc)`.

**`onupdate=datetime.utcnow` only triggers on ORM UPDATE** (`models.py:225`)  
Raw `session.execute(update(...))` calls and Alembic migrations do not trigger `onupdate`. The `updated_at` column will be silently stale on any non-ORM update path.  
*Fix:* Use a PostgreSQL trigger or `server_onupdate=FetchedValue()` with `server_default=func.now()`.

**No range constraint on `confidence` columns** (`models.py:471, 598`)  
`DocumentTag.confidence` and `Relationship.confidence` are documented as `0.0–1.0` but have no `CheckConstraint`. Values outside this range are silently accepted.  
*Fix:* Add `CheckConstraint("confidence >= 0.0 AND confidence <= 1.0")` to both columns.

**Chunk linked list allows cycles** (`models.py:342–373`)  
`prev_chunk_id` and `next_chunk_id` have no constraint preventing self-reference or cycles. Retrieval code that walks the chain would loop infinitely.  
*Fix:* Add `CheckConstraint("id != prev_chunk_id AND id != next_chunk_id")` and document the cycle risk.

**Dead mixin code** (`db/base.py:47–65`)  
`UUIDMixin` and `TimestampMixin` are defined but never used — all models inline their own `id` and `created_at`. These mislead contributors into thinking the mixins are applied.

#### Missing / Incomplete Implementation

**`CacheEntry` has no auto-expiry mechanism**  
Expired rows (where `expires_at < now()`) accumulate silently. No DB-level partial index or trigger purges them.  
*Fix:* Add a scheduled cleanup job or a partial index `WHERE expires_at IS NOT NULL` with a background sweep.

**No `source_path` uniqueness constraint on `Document`**  
Two documents pointing to the same source path with different hashes coexist silently. Only `file_hash` is unique.

#### Test Coverage Gaps

- Transaction rollback on mid-session error
- Duplicate `file_hash` insertion (verify `IntegrityError`)
- Duplicate `category.slug` insertion
- Null constraint violations (missing required fields)
- `confidence` out-of-range values (would expose missing constraint)
- Cascade delete of `Document` removes all child rows
- Large batch inserts (hundreds of chunks)

---

### 3.2 `grimoire/db/session.py`

#### Logic Errors

**`_db_manager` global is not task-safe** (`session.py:96`)  
`initialize_db` replaces the global without a lock. Concurrent startup calls race to set the global, potentially leaking the old engine's connections.  
*Fix:* Guard with `asyncio.Lock` or use `asyncio.get_event_loop().run_until_complete` at startup.

**Double-close in `session()` context manager** (`session.py:80–88`)  
The inner `async with self._session_maker() as session:` already closes the session via `__aexit__`. The `finally: await session.close()` is a redundant second close.

**Alembic port mismatch** (`alembic.ini:8`)  
`alembic.ini` hardcodes `localhost:5434` but `docker-compose.yml` maps `${POSTGRES_PORT:-5432}:5432`. On a default Docker Compose setup, Alembic connects to a different port than the application.  
*Fix:* Use `%(DATABASE_URL)s` in `alembic.ini` and set it from an environment variable.

**Credentials in plain text** (`alembic.ini:8`)  
The Alembic connection string contains `PostgreSQL!` as a literal password. This should reference an environment variable.

---

### 3.3 `grimoire/storage/`

#### Logic Errors

**`local.py`: `_observers` list grows without bound** (`storage/local.py:249`)  
Every `watch()` call appends an observer to `self._observers` but the list is never pruned when `stop()` is called on the handle. Long-running processes leak references to stopped `Observer` objects.

**`local.py`: `watch()` ignores recursive parameter** (`storage/local.py:490`)  
The observer is always scheduled with `recursive=True`, overriding the caller's intent.

**`gdrive.py`: `list_changes()` ignores the `since` parameter** (`storage/gdrive.py:796–858`)  
The `since: datetime` argument is accepted but never used. The method relies solely on page tokens. The contract advertised by the base ABC ("list changes since a given timestamp") is not honoured.

**`gdrive.py`: `__del__` uses deprecated `asyncio.get_event_loop()`** (`storage/gdrive.py:898–902`)  
In Python 3.10+, `asyncio.get_event_loop()` in a non-async finalizer emits a `DeprecationWarning` and may return the wrong loop. The HTTP client will not be closed in most production scenarios.

**`onedrive.py`: `_make_request()` skips token refresh on expired token 401** (`storage/onedrive.py:229–238`)  
After a 401, the code only refreshes if `not self.token_data.is_expired(buffer_seconds=0)`. If the token expired exactly at the buffer, refresh is skipped and the second request also fails with 401.  
*Fix:* Unconditionally refresh the token on any 401 response.

**`onedrive.py`: Delta token key collision** (`storage/onedrive.py:490`)  
`path=None` uses key `"root"` and `path=""` uses key `""` — different entries for logically equivalent inputs. Delta tokens are never reused across these two call patterns.

**`watch_manager.py`: Cloud polling loop is completely non-functional — BLOCKING BUG** (`watch_manager.py:207–213`)  
`_cloud_poll_loop()` updates `last_poll_time`, sleeps, and loops — but never calls `CloudStoragePoller.poll_changes()` or the user's callback. `CloudStoragePoller.poll_changes()` is itself a stub returning `[]`. Cloud watch is entirely broken.

**`watch_manager.py`: MOVED event `previous_path`/`path` are inverted** (`watch_manager.py:361–362`)  
`previous_path=dest_path` is backwards. The destination becomes the "previous" location and vice versa. `local.py`'s handler sets this correctly; `watch_manager.py`'s handler has a copy-paste error.

**`watch_manager.py`: `_local_watch_count` goes negative** (`watch_manager.py:242–251`)  
If `local_observer` is already `None`, the `if active_watch.local_observer:` guard skips the stop but `_local_watch_count -= 1` still executes, potentially going negative and allowing more watchers than `_max_local_watches`.

**`onedrive.py`: Token file not protected** (`storage/onedrive.py:137–149`)  
`_save_tokens()` does not set file permissions (`chmod 0o600`). Google Drive's `_save_tokens()` does. OAuth refresh tokens are left world-readable depending on the umask.

#### Missing / Incomplete Implementation

**`read_file()` loads entire file into memory** — Both `local.py` and `gdrive.py` use non-streaming reads. Files over ~500 MB will OOM the process.

**`onedrive.py` has no `close()` method** — If used without the context manager, the HTTP client is never closed. `GoogleDriveAdapter` has `close()` and `__del__`; `OneDriveAdapter` only has `__aexit__`.

---

### 3.4 `grimoire/vectorstore/chromadb.py`

#### Logic Errors

**Deprecated `chromadb.Client()` API** (`chromadb.py:134`)  
`chromadb.Client()` was replaced by `chromadb.PersistentClient()` in ChromaDB v0.4.x. `docker-compose.yml` uses `chromadb/chroma:latest`, making the version unpredictable.  
*Fix:* Use `chromadb.PersistentClient(path=...)` and pin the ChromaDB version in `pyproject.toml`.

**No remote ChromaDB connection path**  
`GrimoireSettings` has `vector_store.host` and `vector_store.port` fields but `ChromaDBStore.__init__()` never uses them. The Docker-hosted ChromaDB server in `docker-compose.yml` cannot be connected to.  
*Fix:* Add a `chromadb.HttpClient(host=..., port=...)` path when `settings.vector_store.host` is set.

**`search()` does not clamp `top_k` to collection count** (`chromadb.py:321–327`)  
If `top_k > count()`, ChromaDB raises a `ChromaError` with a non-obvious message instead of a user-friendly exception.  
*Fix:* Check `top_k = min(top_k, self.count())` before querying, with a check for empty collections.

**`_validate_distance_metric()` rejects `"l2"` directly** (`chromadb.py:91–97`)  
Passing `"l2"` raises `ValueError` even though ChromaDB accepts it natively. Only `"euclidean"` is mapped to `"l2"` — the direct form is blocked.

**`_format_results()` mutates ChromaDB result objects in-place** (`chromadb.py:443–446`)  
Modifying `metadata[mkey]` in place may affect cached/shared result objects from ChromaDB.  
*Fix:* Create a copy of the metadata dict before mutating: `metadata = dict(row_metadata)`.

#### Test Coverage Gaps

- `search()` with `top_k > collection size`
- `search()` with wrong embedding dimension
- ChromaDB connection failure (invalid persist directory)
- `delete()` of non-existent IDs (raise vs no-op)
- `"l2"` distance metric passed directly

---

### 3.5 `grimoire/core/cache.py`

#### Logic Errors

**`RedisCache.is_connected` doesn't actually ping Redis** (`cache.py:218`)  
Only checks `_client is not None`. After a network partition, `_client` remains non-None but all operations fail. Auto-reconnect only triggers if `_client is None`, so connection drops are not detected.  
*Fix:* Implement `is_connected` with an actual `PING` command, or catch `ConnectionError` in `get()`/`set()` and attempt reconnect.

**`DiskCache.get()` silently swallows all exceptions** (`cache.py:620–623`)  
A corrupted cache file, permission error, or disk full condition is indistinguishable from a cache miss.  
*Fix:* Log errors at `WARNING` level and re-raise known non-miss exceptions, or add a `cache.is_healthy()` method.

**`RedisCache.clear()` uses `KEYS pattern`** (`cache.py:458`)  
`KEYS` is an O(N) blocking command. In a production Redis with millions of keys, this blocks the event loop and Redis server.  
*Fix:* Replace with `SCAN` cursor iteration.

**`CacheFactory.create()` returns an unconnected `RedisCache`** (`cache.py:742–743`)  
Callers must remember to call `await cache.connect()`. This is not obvious from the factory interface.  
*Fix:* Connect inside `create()` (making it async) or document prominently that `connect()` is required.

---

### 3.6 `grimoire/config/settings.py`

#### Logic Errors

**Module-level `settings = get_settings()`** (`settings.py:954`)  
Runs at import time. Any environment with missing required env vars or a malformed `grimoire.yaml` crashes at import, making any code that imports from this module fail with a confusing error.  
*Fix:* Remove the module-level instantiation. Make `get_settings()` the sole entry point, cached with `@lru_cache`.

**`LoggingConfig` creates directories during Pydantic validation** (`settings.py:328–334`)  
Creating a directory as a side effect of model validation is unexpected and violates separation of concerns. Silent failure if the directory cannot be created.

**`EmbeddingConfig.validate_model_name` rejects local model paths** (`settings.py:173–179`)  
Requiring `/` in model names blocks Ollama-style names (`nomic-embed-text`) and local paths (`./models/my-model`).

**`DatabaseConfig.validate_db_url` accepts synchronous `postgresql://`** (`settings.py:209`)  
`create_async_engine` requires `postgresql+asyncpg://`. A `postgresql://` URL passes validation but fails at engine creation.  
*Fix:* Reject any URL that doesn't start with `postgresql+asyncpg://` or `sqlite+aiosqlite://`.

**`CeleryConfig.validate_redis_url` rejects TLS `rediss://`** (`settings.py:369–372`)  
`rediss://` is the standard scheme for Redis TLS connections and should be accepted.

#### Missing / Incomplete Implementation

**No validation that `secret_key` is not the default in production**  
The field comment says "CHANGE IN PROD!" but no validator checks `secret_key != "change-me-in-production"` when `debug=False`.

---

## 4. API & CLI Layer

### 4.1 `grimoire/api/`

#### Logic Errors

**CORS misconfiguration** (`api/main.py:39–40`)  
`allow_origins=["*"]` combined with `allow_credentials=True` is rejected by browsers per the CORS spec. All credentialed cross-origin requests will fail in a browser.  
*Fix:* Either list specific origins or set `allow_credentials=False`.

**Unhandled `ValueError` on invalid `content_type`** (`api/routes/generate.py:26`)  
`ContentType(request.content_type)` raises `ValueError` for invalid values, producing HTTP 500 instead of 400. The `else` branch at line 42 is unreachable because the `ValueError` fires first.  
*Fix:* Wrap in `try/except ValueError` and raise `HTTPException(status_code=400, detail="Invalid content_type")`.

**Wrong HTTP status codes on POST endpoints** (`api/routes/ingest.py:19, 30; api/routes/generate.py:15`)  
`POST /ingest/file`, `POST /ingest/directory`, and `POST /generate` default to 200. Resource creation should return 201.

**`BackgroundTasks` imported but unused** (`api/routes/ingest.py:5`)  
Signals that background task usage was planned but never implemented. `ingest_directory` runs synchronously in the handler, blocking the server during large ingestions.

**`tag_count` and `chunk_count` always 0** (`api/routes/documents.py:49–66`)  
`DocumentResponse` has these fields but they are never populated. Both always return 0.

**Slug generation in categories API lacks sanitization** (`api/routes/categories.py:54`)  
`slug = request.name.lower().replace(" ", "-")` only replaces spaces. Names with accented characters, punctuation, or slashes produce invalid slugs. The CLI uses `python-slugify`; the API should too.

**No uniqueness check before category insert** (`api/routes/categories.py:65–74`)  
A duplicate slug causes a database `IntegrityError` that propagates as HTTP 500 instead of 409 Conflict.

**Agent constructed inside async handler instead of injected via `Depends`** (`api/routes/ingest.py, generate.py`)  
`get_ingestion_agent()` and `get_content_gen_agent()` are called directly inside handler bodies, constructing heavy objects (parser, embedder, vector store) on every request, on the event-loop thread.  
*Fix:* Inject via `agent: Any = Depends(get_ingestion_agent)`.

**`api/dependencies.py` imports from `grimoire.cli.helpers`**  
Creates a circular architectural dependency (api → cli). Builder functions should live in a shared `grimoire/core/factory.py`.

**Route ambiguity: `DELETE /watch/{watch_id}` vs `GET /watch/status`** (`api/routes/watch.py:45, 54`)  
A `DELETE /watch/status` request would match `DELETE /{watch_id}` with `watch_id="status"`, silently deleting a watch named "status". The route should be restructured (e.g., `DELETE /watch/{watch_id}/stop`).

#### Test Coverage Gaps (`tests/test_api.py`)

- 422 responses for all endpoints with missing/invalid required fields
- Agent exception propagation → 500 for query, ingest, and generate routes
- `POST /categories` success (201), duplicate slug (409), and missing name (422)
- `POST /watch/start` and `GET /watch/status` with a mocked watcher
- `GET /documents` pagination (`offset`, `limit` query params)
- `GET /documents` with `status` or `file_type` filter params
- `DELETE /documents/{id}` success (204)
- `DELETE /categories/{id}` success (204)
- **`test_generate_unsupported_type` asserts `status_code in (400, 422, 500)`** — too permissive; the actual bug (500 not 400) is masked by this assertion

---

### 4.2 `grimoire/cli/`

#### Logic Errors

**`cli/ingest.py`: `--recursive` flag has no effect** (`cli/ingest.py:22`)  
`is_flag=True, default=True` means the flag toggles to `True` when present, but the default is already `True`. The flag cannot be used to disable recursion. Use `--recursive/--no-recursive` paired form.

**`cli/ingest.py`: `--strategy` is silently ignored** (`cli/ingest.py:23, 27`)  
`--strategy` is accepted as a CLI option but never passed to the agent. Users who specify `--strategy semantic` see no behavioral change.

**`cli/generate.py`: `_resolve_doc_ids` returns empty list instead of failing** (`cli/generate.py:58–60`)  
When no documents are found, `echo_error(...)` is called but the function returns `[]`. Callers silently exit with code 0, indistinguishable from a successful run.  
*Fix:* Call `raise click.Abort()` or `sys.exit(1)` after the error message.

**`cli/categories.py`: `--force` flag has no functional effect** (`cli/categories.py:128, 150`)  
The flag is declared but the implementation deletes the category unconditionally regardless of whether `force` is `True` or `False`. Without `--force`, the command should refuse to delete a category with tagged documents.

**`cli/categories.py`: `tag` commits even when all tags were missing** (`cli/categories.py:198`)  
`await db.commit()` and `echo_success` always run even if every `tag_name` was not found (all loop iterations hit `continue`). A no-op commit is reported as success.

**`cli/categories.py`: `untag` reports success when all tags are missing** (`cli/categories.py:241`)  
`echo_success` runs unconditionally even if all `echo_error` paths fired.

**`cli/watch.py`: `watch_unwatch` is a stub** (`cli/watch.py:100–111`)  
Accepts a `WATCH_ID` argument but only prints a message saying to use Ctrl+C. Does not stop any watcher.

**`cli/watch.py`: `watch_list` is a stub** (`cli/watch.py:87–97`)  
Reports that watches are "only visible within a running process." Returns no actionable information.

**`cli/docs.py`: `_output_text` column width truncates long file types** (`cli/docs.py:143`)  
File type column is only 6 characters wide. `"markdown"` (8 chars) overflows and misaligns all subsequent columns.

**`cli/helpers.py`: `build_query_agent` omits `fallback_model`** (`cli/helpers.py:101–105`)  
`build_ingestion_agent` passes `fallback_model=settings.embeddings.fallback_model` but `build_query_agent` does not. If the primary embedding model fails during queries, there is no fallback — inconsistent resilience.

**`generate extract` subcommand missing from CLI**  
The API supports `content_type: "extract"` but there is no `extract` subcommand registered under `@generate.command()`. Users cannot access extract generation via the CLI.

#### Test Coverage Gaps (`tests/test_cli.py`)

- **No tests for any `watch` subcommand** (`watch start`, `watch list`, `watch unwatch`)
- **No tests for `tag` or `untag` commands**
- **No tests for `category add`, `category list`, `category remove`**
- No test for `ingest --strategy` (would expose the silent-ignore bug)
- No test for `ingest` with `status="failed"` result
- `--format json/table/markdown` for all list commands
- `test_version` hardcodes `"2.0.0"` — will fail on any other release; should use `from grimoire import __version__`
- Error messages written to stderr (tests check `result.output` which is stdout only)

---

## 5. Search Layer

### 5.1 `grimoire/search/fulltext.py`

#### Logic Errors

**SQL injection risk via `to_tsquery`** (`fulltext.py:272, 377, 446`)  
`func.to_tsquery(self.language, parsed_query)` passes the parsed string as a literal SQL argument. A malformed or adversarially crafted string can produce PostgreSQL syntax errors or, in edge cases, permit injection.  
*Fix:* Use `text("to_tsquery(:lang, :q)").bindparams(lang=self.language, q=parsed_query)` to ensure proper parameterization.

**`ts_headline` embeds user-supplied `fragment_delimiter` in raw SQL** (`fulltext.py:454–457`)  
An f-string embeds `fragment_delimiter` directly into a `text(...)` SQL string. A delimiter containing a single quote creates a SQL syntax error or injection.  
*Fix:* Pass `fragment_delimiter` as a bound parameter.

**`escape_special_chars` gives false security** (`fulltext.py:79–95`)  
The docstring claims it escapes FTS operator characters (`! & | ( ) : * '`) but the implementation only escapes backslashes and single quotes. Characters like `!`, `&`, `|`, `(`, `)`, `:`, `*` pass through unescaped and are interpreted as tsquery operators.  
*Fix:* Escape all FTS operator characters, or use `plainto_tsquery` for user-controlled input (which handles escaping internally).

**Phrase handling allows FTS operators inside quoted phrases** (`fulltext.py:124–129`)  
Characters like `&` inside a quoted phrase (e.g., `"hello & world"`) are not escaped before being joined with `<->`, producing invalid tsquery syntax.

**`search_chunks_only` is a near-duplicate of `search`** (`fulltext.py:356–410`)  
Re-implements the full search body independently. Bug fixes in `_execute_search` will not apply to `search_chunks_only`.  
*Fix:* Delegate `search_chunks_only` to `_execute_search` with `include_title_weight=False`.

**`tsvector_expr` computed but never used** (`fulltext.py:447`)  
`tsvector_expr` is computed but not referenced in the `ts_headline` call. Dead code that was likely intended to be passed to `ts_headline` for performance.

#### Test Coverage Gaps (`tests/test_fulltext.py`)

- SQL injection / dangerous FTS characters through the full `FulltextSearch.search()` call
- `search()` with `document_ids` filter (public parameter with zero test coverage)
- `search()` with `top_k=0`
- Results where `row.rank is None`
- `highlight()` with empty query string
- `highlight()` where `ftq.parsed` is empty
- Unicode queries through `FulltextSearch.search()` (only `parse_query` is tested)
- Result ordering verification (current test pre-sorts mock data — doesn't verify `ORDER BY rank DESC`)

#### Documentation Issues

**Module docstring claims "hybrid search capability"** (`fulltext.py:4`)  
This module is FTS-only. Hybrid search lives in `hybrid.py`.

**`FTSQuery` is labeled "Pydantic Models"** (`fulltext.py:23`)  
`FTSQuery` and `FTSResult` are `@dataclass`, not Pydantic models.

---

### 5.2 `grimoire/search/hybrid.py`

#### Logic Errors

**Parallel search is actually sequential** (`hybrid.py:133–136`)  
The docstring says "performs both searches in parallel" but the implementation uses sequential `await` calls. A slow vector search blocks the FTS search.  
*Fix:* `vector_results, fts_results = await asyncio.gather(self._vector_search(...), self._fts_search(...))`

**Scoring asymmetry when one source returns empty** (`hybrid.py:289, 242`)  
When vector search returns results but FTS returns empty (or vice versa), the combined scores are not comparable. A vector-only result scores `0.7 * similarity`; an FTS-only result scores `0.3 * fts_rank`. A mediocre FTS match can outrank an excellent vector-only match.  
This is a design decision, but the asymmetric behavior is completely undocumented.

**ChromaDB distance `1.0 - distance` can be negative** (`hybrid.py:233`)  
Cosine distance in ChromaDB ranges `[0, 2]`. `1.0 - distance` yields negative values for very dissimilar vectors. The `max(0.0, ...)` clamp silently hides these.

**`_apply_reranking` permanently mutates `HybridResult.score`** (`hybrid.py:374–375`)  
Replaces the weighted hybrid score with a positional reranking score. The original score is lost.  
*Fix:* Add a `HybridResult.rerank_score` field and preserve the original `score`.

**`_fts_search` normalizes ranks against the current result set only** (`hybrid.py:283`)  
Min-max normalization within a batch means all poor FTS results get scaled to the same range as excellent ones. A result set where all FTS ranks are low will produce a best result with `normalized_score=1.0`, making it appear to rival strong vector matches.

#### Missing / Incomplete Implementation — **CRITICAL**

**No tests exist for `hybrid.py`**  
`HybridSearch`, `_merge_results`, `_vector_search`, `_fts_search`, and `_apply_reranking` have zero test coverage. This is the most significant coverage gap in the search layer.

**`HybridSearch` not exported from `grimoire.search`**  
`grimoire/search/__init__.py` only exports `fulltext.py` symbols. Callers must import directly from `grimoire.search.hybrid`, breaking the intended public API.

---

## 6. Cross-Cutting Concerns

### 6.1 Exception Swallowing

The following locations use `except Exception: pass` or catch-all patterns that lose diagnostic information:

| Location | Issue |
|----------|-------|
| `cli/status.py:79–80` | Cache stats errors silently swallowed — shows nothing to user |
| `storage/gdrive.py:527–528` | JSON error body discarded during auth token exchange |
| `vectorstore/chromadb.py:146` | Non-`ChromaError` exceptions from `get_or_create_collection` swallowed |
| `search/hybrid.py:250–252, 298–300, 381–383` | Search failures return empty results — caller cannot distinguish "no results" from "crashed" |
| `core/cache.py:620–623` | `DiskCache.get()` swallows all exceptions — corrupt cache indistinguishable from miss |
| `core/cache.py:667–668` | `DiskCache.delete()` swallows all exceptions — failed deletes silently allow stale entries |

### 6.2 `print()` Statements in Production Code

The following `print()` calls should be replaced with `logger.*`:

| Location | Issue |
|----------|-------|
| `agents/content_gen.py:145` | `print(result.content)` in live code path |
| `agents/coordinator.py:25, 277` | `print(result.intent, ...)` and `print(result.result.content)` |
| `agents/query.py:122, 124` | `print(result.answer)` and source output |
| `core/parser.py:392–393, 579` | `print(...)` statements |

### 6.3 Async Consistency

| Location | Issue |
|----------|-------|
| `search/hybrid.py:133–136` | Sequential `await` instead of `asyncio.gather` (documented as parallel) |
| `storage/gdrive.py:897–901` | `asyncio.get_event_loop().create_task()` in `__del__` — deprecated and unreliable |
| `core/dedup.py:227` | Synchronous `compute_file_hash` called in `async check_file` — blocks event loop |
| `core/chunker/semantic.py` | ML model loaded/run synchronously inside `async def chunk()` |
| `core/parser.py:453` | `asyncio.get_event_loop()` should be `asyncio.get_running_loop()` |

### 6.4 Type Hints

| Location | Issue |
|----------|-------|
| `utils/logger.py:94` | `get_logger() -> Any` — should return `loguru.Logger` |
| `search/fulltext.py:255` | Mix of `list[FTSResult]` (lowercase) and `List[FTSResult]` (typing import) |
| `search/hybrid.py:214` | `_vector_store.is_initialized` accessed via `hasattr` duck-typing, bypassing type safety |

### 6.5 Logger Side Effects

**`setup_logger()` is called at module import time** (`utils/logger.py:107`)  
- Creates a `./log/` directory relative to the current working directory
- Calls `logger.remove()`, resetting any handlers previously configured (including test fixtures)
- Starts a background `enqueue=True` thread that is never stopped, causing spurious errors on process exit
- `grimoire/cli/main.py:45` then calls `logger.remove()` again in the CLI group callback, creating handler lifecycle inconsistencies

*Fix:* Remove the module-level `setup_logger()` call. Call it explicitly from `__main__` or the app factory.

### 6.6 `pyproject.toml` Issues

**`pytest-httpx` missing from dev dependencies**  
`tests/test_storage_onedrive.py:17` imports `from pytest_httpx import HTTPXMock` but `pytest-httpx` is not listed in `[project.optional-dependencies].dev`. Tests fail on a fresh dev setup with `ImportError`.

**Duplicate dev dependency groups**  
`[project.optional-dependencies].dev` (line 52) and `[dependency-groups].dev` (line 239) both define dev dependencies with overlapping but different sets. `pytest-asyncio` appears in both with different version constraints.

**`chromadb>=0.4` version range too wide**  
ChromaDB has breaking API changes between 0.4.x and 0.5.x. Pin with `chromadb>=0.4,<0.5` until the `PersistentClient` migration is complete.

---

## 7. Documentation Issues

### 7.1 `Claude.md`

| Issue | Location | Fix |
|-------|----------|-----|
| Wrong uvicorn module path | `Claude.md:46` | Change `app.main:app` → `grimoire.api.main:app` |
| Wrong black target directory | `Claude.md:49` | Change `uv run black app tests` → `uv run black grimoire tests` |
| `npm test` / `npm run typecheck` are irrelevant | `Claude.md:63–65` | Remove Node.js commands from this pure Python project |
| Inconsistent package install commands | `Claude.md:34, 45` | Remove `uv pip install` (line 45); use `uv add` consistently |
| File truncated mid-sentence | `Claude.md:60` | Complete the sentence ("Run `ruff check .` and `black` before com...") |
| States "Main app code in `app/`" | `Claude.md:20` | The package is `grimoire/`, not `app/`. Update description. |

### 7.2 `DESIGN.md` vs Implementation

| Design claim | Actual implementation | Status |
|-------------|----------------------|--------|
| pgvector hybrid search | Pure Python score fusion in `hybrid.py` | Not implemented |
| Qdrant as vector store option | Only `chromadb.py` exists | Not implemented |
| Structured JSON log output | Human-readable `DEFAULT_LOG_FORMAT` in `logger.py` | Deviation from spec |
| Log directory `/log` during dev | `./log` relative to CWD | Inconsistent with spec |
| "performs both searches in parallel" | Sequential `await` calls | Documented but incorrect |

### 7.3 `VALIDATION_RESULTS.md`

The validation results cover only **Phase 1, Task 1.1** (project skeleton, completed 2026-03-29, 9 files). The current codebase has 60+ files across 6 phases. The validation document is **outdated** and does not reflect the current state of the project. No validation records exist for any phase after the initial skeleton.

### 7.4 `README.md`

- The `grimoire docs list --since 7d` relative-time notation is shown but not tested anywhere
- Category slug examples (`--parent research`, then `--parent ai-ml`) are inconsistently documented

---

## 8. Global TODO/FIXME Inventory

Only **one** explicit marker was found in the entire `grimoire/` source tree. The absence of markers does not mean the code is complete — multiple unfinished features have no annotation.

| File | Line | Comment | Priority |
|------|------|---------|----------|
| `grimoire/config/settings.py` | 682 | `# noqa: S104` — suppresses Bandit "Possible binding to all interfaces" for `host: str = Field(default="0.0.0.0")` | Trivial — acceptable for dev default; ensure production overrides via env var |

**Unannoted incomplete work** (no marker, but effectively TODO):

| Feature | Location | Status |
|---------|----------|--------|
| `CrossEncoderReranker` implementation | `core/reranker.py` | Missing entirely |
| Cloud watch polling | `storage/watch_manager.py` | Stub returning `[]` |
| `TaggingResult.cached` caching | `agents/../core/tagger.py` | Field exists, always `False` |
| `_log_extraction` audit logging | `agents/ingestion.py` | Method exists, always `pass` |
| Watch persistence to DB | `agents/watcher.py` | `WatchPath` imported, never used |
| `generate extract` CLI subcommand | `cli/generate.py` | Exists in API only |
| Remote ChromaDB connection | `vectorstore/chromadb.py` | Config exists, never used |
| Qdrant vector store implementation | `vectorstore/` | Documented in DESIGN.md, not implemented |
| pgvector hybrid search | `search/` | Documented in DESIGN.md, not implemented |
| `watch unwatch` / `watch list` CLI | `cli/watch.py` | Stubs printing informational messages |

---

## 9. Priority Fix List

### P0 — Blocking (breaks core functionality)

1. **`reranker.py`: Implement `CrossEncoderReranker`** — the retrieval pipeline references this class but it does not exist. Any code path that reaches the reranker raises `AttributeError`.
2. **`watch_manager.py`: Fix cloud watch polling loop** — the loop runs but never invokes the adapter or callback. Cloud watch is entirely non-functional.
3. **`content_gen.py` / `query.py`: Stop persisting LLM error strings to DB/cache** — transient failures poison the database and cache with 30-day TTL. Add `is_error` flag or check before persisting.
4. **`api/routes/generate.py`: Catch `ValueError` on invalid `content_type`** — currently returns HTTP 500; should return 400.
5. **`config/settings.py`: Remove module-level `settings = get_settings()`** — breaks all imports in environments with missing env vars.
6. **`pyproject.toml`: Add `pytest-httpx` to dev dependencies** — `test_storage_onedrive.py` fails with `ImportError` on a fresh checkout.

### P1 — Important (correctness / data integrity)

7. **`db/models.py`: Fix `datetime.utcnow` throughout** — replace with `datetime.now(tz=timezone.utc)` or `server_default=func.now()`. Affects every timestamp column.
8. **`dedup.py`: Fix timezone-aware vs naive datetime comparison** — causes `TypeError` at runtime when comparing `Document.updated_at` with file mtime.
9. **`agents/ingestion.py`: Add `db.rollback()` on partial failure** — orphaned rows and stuck documents accumulate on any pipeline error.
10. **`search/fulltext.py`: Fix SQL injection risk in `to_tsquery`** — use parameterized binding, not string literal.
11. **`search/fulltext.py`: Fix `escape_special_chars`** — currently only escapes backslashes and single quotes; FTS operator characters pass through unescaped.
12. **`search/hybrid.py`: Use `asyncio.gather` for parallel search** — documented as parallel but runs sequentially, doubling latency.
13. **`api/main.py`: Fix CORS `allow_origins=["*"]` with `allow_credentials=True`** — all browser credentialed requests fail.
14. **`alembic.ini`: Fix port mismatch with docker-compose** — migrations connect to a different port than the application on a default setup.
15. **`storage/onedrive.py`: Protect token file with `chmod 0o600`** — OAuth refresh tokens are world-readable.
16. **`cli/watch.py`: Implement `watch unwatch` and `watch list`** (or mark as stubs in help text)

### P2 — Should Fix (quality / completeness)

17. **Write tests for `hybrid.py`** — zero test coverage on the core search fusion logic
18. **`chunker/markdown.py`: Fix header detection order** (use sorted list, not `set`)
19. **`chunker/recursive.py`: Fix `_recursive_split` content reordering**
20. **`chunker/semantic.py`: Run ML model in thread pool executor**
21. **`tagger.py`: Add cycle detection in `build_path`**
22. **`tagger.py`: Close HTTP client in `finally` block**
23. **`vectorstore/chromadb.py`: Migrate to `PersistentClient()` and add remote connection path**
24. **`cache.py`: Replace `KEYS` with `SCAN` in `RedisCache.clear()`**
25. **`cli/ingest.py`: Fix `--recursive` flag** (use `--recursive/--no-recursive` paired form)
26. **`cli/categories.py`: Implement `--force` flag behavior**
27. **`Claude.md`: Fix all incorrect commands** (uvicorn path, black target, remove npm commands)
28. **`VALIDATION_RESULTS.md`: Update to reflect current codebase state**
29. **Export `HybridSearch` from `grimoire.search.__init__`**
30. **`parser.py`: Forward `enable_tables`, `enable_figures`, `ocr_enabled` to Docling converter**
