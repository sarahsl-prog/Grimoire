# Code Review Report — Grimoire

**Date:** 2026-09-01
**Branch reviewed:** `mcp-updates` (commit `011cb1b`, PR #57)
**Reviewer:** Automated code review
**Scope:** MCP tools, security strategy (corpus detection, chunking, parsing, metadata), database models, API routes, tests, documentation

---

## Priority Overview

| Priority | Count | Label |
|----------|-------|-------|
| P1 — Bug | 3 | Immediate correctness issues |
| P2 — Logic Error | 4 | Unlikely to crash but produces wrong results |
| P3 — Naming/Consistency | 3 | Confusing API surface, maintenance hazard |
| P4 — Test Gap | 4 | Missing coverage for edge cases |
| P5 — Documentation | 2 | Docs disagree with code |
| P6 — Tech Decision | 4 | Open questions requiring team input |

---

## P1 — Immediate Correctness Issues

### P1.1 — `_jsonb_list_contains` pattern matches values inside other strings

**File:** `grimoire/mcp/tools.py:369`

```python
return cast(column, String).ilike(f'%"{value}"%')
```

**Problem:** When `playbook_phase` is stored as a scalar string in the JSONB blob (e.g., `"contain"`), the `ILIKE '%"contain"%'` pattern works correctly. However, if any JSONB list field uses the pipe-join format (e.g., `"platforms": "windows|linux"`), searching for `"windows"` via this pattern would also match values *inside other words* — e.g., it would falsely match `"somesource"` if that existed in any JSONB cell, because the pattern is anchored by literal quotes but those quotes may or may not exist depending on how the value was serialised into JSONB.

More critically: when `platform` is passed to `_jsonb_list_contains(Document.security_metadata, params.platform)`, if the actual stored value is a JSON array (which SQLAlchemy would serialize differently than a pipe-joined string), the `%"windows"%` pattern could miss or mis-match it.

The ChromaDB metadata path uses `to_chromadb_metadata()` which pipe-joins lists and stores them as plain strings — there the ILIKE pattern works fine. But `grimoire_search_playbook` uses both the SQL path (`Document.security_metadata` directly, via `_jsonb_list_contains`) and the ChromaDB metadata path (via `agent.search(..., filter_dict=filters)`). These two paths can be out of sync: the SQL path searches the raw JSONB blob while the ChromaDB path searches ChromaDB's own metadata representation.

**Impact:** If a Sigma rule's `platforms` list is stored as a JSON array `["windows", "linux"]` in PostgreSQL JSONB (via SQLAlchemy's JSON serialization), the ILIKE pattern on the raw JSONB string `'["windows", "linux"]'` would still match `'windows'` (it would match `'windows"` inside the string) — so this particular case works. But if any value contains the search term as a *substring* of an unrelated string, it would be a false positive.

**Recommended fix:** Use PostgreSQL's native JSONB containment or equality operators instead of ILIKE. Replace `_jsonb_list_contains` with a proper JSONB extraction + equality check:

```python
from sqlalchemy import func, Text

def _jsonb_list_contains(column: Any, value: str) -> Any:
    """Match a scalar string value in a JSONB column using JSONB → text cast."""
    from sqlalchemy import cast, String
    return cast(column, String).ilike(f'%"{value}"%')
```

Or better, for PostgreSQL-specific deployments, use:

```python
from sqlalchemy import func

def _jsonb_list_contains(column: Any, value: str) -> Any:
    """PostgreSQL JSONB: check if any array element equals the value."""
    return func.jsonb_path_query_array(column, f'$.** ? (@ == "{value}")').cast(String).isnot(None)
```

For cross-database compatibility, the current ILIKE approach is acceptable as a workaround, but a comment should be added documenting the assumption that values are stored as scalar strings, not JSON arrays.

---

### P1.2 — `grimoire_search_playbook` search path ignores `phase` when `query` is absent

**File:** `grimoire/mcp/tools.py:563`

```python
query_text = params.query or "*"
```

**Problem:** When `query` is absent, `query_text` is set to `"*"`. The `_facet_search` function then passes `"*"` to `agent.search(..., query_text="*")`. A wildcard `*` query is typically handled by the query agent as "match all documents in the filter scope". However, if the agent's search implementation does not handle `*` specially and treats it as a literal term, the search would return zero results (since no document contains a chunk with the literal text `"*"`).

The `_facet_search` short-circuits when `doc_ids` is empty, but when it is non-empty it calls `agent.search` with `query_text="*"`. This works only if the query agent treats `"*"` as "return all documents in scope". If the underlying vector store (ChromaDB) receives a `query_text="*"` it may return unpredictable results or error.

**Recommended fix:** Add an explicit short-circuit in `_facet_search` for the wildcard query case — return the filtered document IDs directly as results without invoking the vector search:

```python
if query_text == "*":
    # Structured-facet-only search: return all matching doc IDs without vector scoring.
    # (The doc_ids list already reflects the SQL pre-filter result.)
    # Fall through to the agent search for now, but this could be optimised
    # by bypassing the vector layer entirely.
    pass
```

Alternatively, add a dedicated `grimoire_list_documents`-style path that returns pre-filtered IDs without a vector round-trip.

---

### P1.3 — `grimoire_delete_document` uses `doc.chunks` after `await db.delete(doc)` but `doc.chunks` is a lazy relationship

**File:** `grimoire/mcp/tools.py:877`

```python
await db.delete(doc)
try:
    await db.commit()
except Exception:
    await db.rollback()
    return _err("Failed to delete document.")

try:
    from grimoire.services.vector_store import get_vector_store_service
    vector_ids = [chunk.vector_id for chunk in doc.chunks if chunk.vector_id]
    if vector_ids:
        await vector_store.delete_vectors(vector_ids)
```

**Problem:** After `db.delete(doc)` and `db.commit()`, the `doc` object is in a "deleted" state. Accessing the `doc.chunks` relationship (which is lazily loaded by default in SQLAlchemy) may raise `DetachedInstanceError` or return an empty list, because the session has committed and the object is no longer attached. The vector IDs would either be empty or cause an exception, leaving orphaned vectors in ChromaDB.

**Recommended fix:** Collect the vector IDs *before* deleting the document:

```python
vector_ids = [chunk.vector_id for chunk in doc.chunks if chunk.vector_id]
await db.delete(doc)
try:
    await db.commit()
except Exception:
    await db.rollback()
    return _err("Failed to delete document.")
# Vector cleanup after durable commit
if vector_ids:
    try:
        vector_store = get_vector_store_service(settings)
        await vector_store.delete_vectors(vector_ids)
    except Exception as e:
        logger.warning(f"Vectors not cleaned up for {params.document_id}: {e}")
```

---

## P2 — Logic Errors

### P2.1 — `log_sources` stored in JSONB but never emitted to ChromaDB metadata

**File:** `grimoire/strategies/security/metadata.py:305` (`to_chromadb_metadata`)

```python
payload: dict[str, Any] = {
    # ... other fields ...
    # NOTE: log_sources intentionally omitted from ChromaDB metadata
    #       because ChromaDB $contains on pipe-joined strings is unreliable.
}
```

**Impact:** `grimoire_search_playbook` accepts a `log_source` filter parameter and emits it as a SQL condition via `_jsonb_list_contains(Document.security_metadata, params.log_source.lower())`. This works for SQL-side filtering. However, the ChromaDB-side filter dict constructed in `_facet_search` contains:

```python
filters: Dict[str, Any] = {
    "source_type": ...,
    "document_id": {"$in": doc_ids}
}
```

There is **no `log_source` / `log_sources` key in the ChromaDB filter**. The vector search therefore does not post-filter by `log_source` — it only relies on the SQL pre-filter. This is intentional (the comment explains it), but undocumented in the tool docstring. A user filtering by `log_source` expects vector-layer post-filtering as well.

If the SQL pre-filter narrows to `doc_ids` and then the ChromaDB query returns fewer results because of its own scoring, the `log_source` filter is effectively applied twice (SQL then vector), which is correct but inefficient. The vector-layer filtering is never applied because `log_sources` is not in the ChromaDB metadata.

**Recommended fix:** Either (a) add `log_sources` to ChromaDB metadata (pipe-joined string, same as platforms), or (b) clearly document in the `PlaybookSearchInput` docstring that `log_source` is a SQL-side pre-filter only. Option (a) is preferable for consistency.

### P2.2 — `PlaybookSearchInput.platform` is single string but `SecurityMetadata.platforms` is a list

**File:** `grimoire/mcp/tools.py:255` vs `grimoire/strategies/security/metadata.py:201`

The `PlaybookSearchInput.platform` field accepts a single `Optional[str]` (e.g., `"windows"`). The SQL filter:

```python
if params.platform:
    conditions.append(_jsonb_list_contains(Document.security_metadata, params.platform.lower()))
```

This searches the `security_metadata` JSONB for the platform string. The `to_chromadb_metadata()` method emits `platforms` as a pipe-joined string: `"windows|linux"`. So:

- SQL path: searches raw JSONB for `"windows"` — works
- ChromaDB path: ChromaDB filter dict does **not** include a `platforms` key (see P2.1), so the vector search does not filter by platform at all

Additionally, if a document has `platforms: ["windows", "linux"]` (stored as a JSON array in JSONB), the ILIKE pattern `'%"windows"%'` would match the string `'["windows", "linux"]'` correctly. But if the value is stored as the pipe-joined string `'windows|linux'`, the pattern `'%"windows"%'` would also match. So in practice this works, but the logic is fragile.

**Recommended fix:** Same as P2.1 — add `platforms` to ChromaDB metadata explicitly.

### P2.3 — `_facet_search` returns `"matched_documents"` as count of pre-filtered IDs, not final results

**File:** `grimoire/mcp/tools.py:440`

```python
"matched_documents": len(doc_ids),
```

When the SQL pre-filter returns 500 document IDs and the ChromaDB vector search (restricted to those 500) returns only 10 results, the response says `"matched_documents": 500` but `"total_results": 10`. This is confusing — `matched_documents` sounds like it should be the count of documents matched by the vector search, not the count of IDs passed to the vector layer.

**Recommended fix:** Rename the field to `"sql_prefiltered_documents"` or `"candidate_documents"` to clarify that this is the SQL pre-filter cardinality, and add a `"vector_results"` field to distinguish it from `total_results`.

### P2.4 — MITRE ATT&CK chunker docstring says "Phase 5" but Phase 5 is past

**File:** `grimoire/strategies/security/chunker.py:70`

```python
* ``mitre_attack`` → raise :class:`NotImplementedError` until Phase 5.
```

The chunker is marked as Phase 4 in its module docstring, and the MITRE chunker (`_chunk_mitre`) is fully implemented. This comment is stale.

**Recommended fix:** Remove the `NotImplementedError` clause from the docstring since MITRE ATT&CK chunking is implemented.

---

## P3 — Naming / Consistency Issues

### P3.1 — `log_source` (singular) in MCP tool vs `log_sources` (plural) in `SecurityMetadata`

**Files:**
- `grimoire/mcp/tools.py:258` — `log_source: Optional[str]` (input field)
- `grimoire/mcp/tools.py:554` — `params.log_source` used in SQL filter
- `grimoire/strategies/security/metadata.py:205` — `log_sources: List[str]` (model field)
- `grimoire/strategies/security/parsers/sigma.py:286` — extracts `log_sources`

**Problem:** The MCP tool parameter is named `log_source` (singular), consistent with Sigma's singular `logsource` concept. However, the `SecurityMetadata` model uses `log_sources` (plural), and `to_chromadb_metadata()` omits it entirely (see P2.1). This creates an API surface inconsistency where a user filtering by `log_source` cannot verify the value exists in the output metadata.

**Recommended fix:** Add `log_sources` to `to_chromadb_metadata()` as a pipe-joined string (matching the pattern used for `platforms`, `cwe_ids`, etc.), or rename the MCP field to `log_sources` for consistency with the model. Prefer the former (adding to ChromaDB metadata) for backward compatibility.

### P3.2 — `SearchInput.filter_dict` is `Optional[Dict[str, Any]]` with no schema enforcement

**File:** `grimoire/mcp/tools.py:77`

```python
filter_dict: Optional[Dict[str, Any]] = Field(
    default=None, description="Optional metadata filters."
)
```

The `filter_dict` is passed directly to `agent.search(..., filter_dict=params.filter_dict)`. If the agent's search implementation passes `filter_dict` to ChromaDB, invalid filter keys will cause ChromaDB errors at runtime. No validation is performed on `filter_dict`'s structure.

**Recommended fix:** Either (a) document the expected `filter_dict` schema in the tool docstring, or (b) validate known keys and reject unknown ones with a clear error message. At minimum, add a comment documenting which keys are supported.

### P3.3 — `facet_label` parameter in `_facet_search` is unused

**File:** `grimoire/mcp/tools.py:395`

The `facet_label` parameter is passed to `_facet_search` from `grimoire_search_cve` (`"CVE"`) and `grimoire_search_playbook` (`"playbook"`) but is never used in the function body. The `_ok` response dictionary does not include it.

**Recommended fix:** Either remove the parameter or include it in the response dict:

```python
return _ok({
    "facet_label": facet_label,
    # ... existing fields
})
```

---

## P4 — Test Gaps

### P4.1 — No test for `grimoire_search_playbook` with `source_types="sigma"` (sigma-only)

**File:** `tests/test_mcp.py`

The test `test_search_playbook_default_covers_both_corpora` verifies `source_types="all"` produces the correct `Document.source_type.in_(["playbook", "sigma_rule"])` filter. The test `test_search_playbook_facet_filters_docs_in_sql` uses `source_types="sigma"`. However, the sigma-only filter path (single-element `source_types` list, using `==` not `in_`) is only tested implicitly through the mock. A direct assertion that `source_types="sigma"` produces `Document.source_type == "sigma_rule"` (not `in_(["sigma_rule"])`) is missing.

**Recommended fix:** Add a test `test_search_playbook_sigma_only_uses_equality` that verifies the SQL generated for sigma-only source type uses `==` instead of `IN`.

### P4.2 — No test for `grimoire_search_playbook` with no matching documents (empty SQL result)

**File:** `tests/test_mcp.py`

The CVE search has `test_search_cve_no_matching_docs_short_circuits` verifying that when SQL pre-filter returns no document IDs, the response is an empty results array without calling the vector store. The playbook search has no equivalent test.

**Recommended fix:** Add `test_search_playbook_no_matching_docs_short_circuits` mirroring the CVE test.

### P4.3 — No integration test for playbook corpus detection + parsing + chunking pipeline

**Files:** `tests/strategies/test_playbook_parser.py`, `tests/strategies/test_security_chunker.py`

Both parsers and chunkers are tested in isolation, but there is no end-to-end test that:
1. Takes a real playbook markdown file
2. Runs it through `detect_source_type`
3. Chunks it through `SecurityChunker`
4. Verifies the resulting chunk metadata contains the correct `playbook_phase`, `action_type`, `trigger`, `mitre_technique_id`

The smoke test in the session worked manually but is not committed as a regression test.

**Recommended fix:** Add `tests/strategies/test_playbook_corpus_e2e.py` that uses the fixture files in `tests/fixtures/security/playbooks/` and verifies the full pipeline.

### P4.4 — `grimoire_pg_query` bound-parameter `LIMIT` uses `text()` with `bindparams` but `inner_query` is interpolated as a raw string

**File:** `grimoire/mcp/tools.py:899`

```python
sql = text(
    "SELECT * FROM (:inner_query) AS _grimoire_q LIMIT :row_limit"
).bindparams(inner_query=inner, row_limit=params.limit)
```

**Problem:** The `text()` construct with `:inner_query` placeholder uses SQLAlchemy's `bindparams`. However, `inner` is the user-supplied SQL with leading/trailing whitespace stripped but otherwise raw. When used as a subquery, if the user-supplied SQL does not have a trailing semicolon, some PostgreSQL versions may accept it while others require one. More importantly, wrapping a `SELECT` statement that itself contains a `LIMIT` in a subquery is valid SQL, but wrapping a `WITH ... SELECT` CTE may not work with the `(:inner_query)` AS-alias syntax in all PostgreSQL versions (CTEs need parentheses in some contexts).

The validator rejects `SELECT INTO` but does not reject a query that has its own `LIMIT`. A query with `LIMIT 5` wrapped in this subquery would silently ignore the outer `LIMIT :row_limit` if the DBMS processes the inner limit first.

**Recommended fix:** Either (a) strip `LIMIT ...` from the inner query before wrapping, or (b) add a note to the error message that the outer `LIMIT` is always applied and inner `LIMIT` clauses are ignored.

---

## P5 — Documentation Issues

### P5.1 — `PlaybookSearchInput` docstring still describes Sigma-only search

**File:** `grimoire/mcp/tools.py:234`

```python
class PlaybookSearchInput(BaseModel):
    """Parameters for grimoire_search_playbook.

    Searches the Sigma detection-rule corpus ("playbooks") with optional
    MITRE ATT&CK, platform, log-source, and severity facets.
    """
```

The docstring says "Sigma detection-rule corpus ("playbooks")" and mentions only Sigma. With `source_types` support, the tool now searches both playbooks and Sigma rules. The docstring should be updated to reflect this.

**Recommended fix:**
```python
"""Parameters for grimoire_search_playbook.

Searches the Sigma detection-rule and IR-playbook corpora with optional
MITRE ATT&CK technique, IR phase, platform, log-source, and severity facets.
Use ``source_types`` to restrict to ``"playbooks"`` or ``"sigma"`` only
(default: ``"all"``, covering both).
"""
```

### P5.2 — `grimoire_search_playbook` tool docstring doesn't mention `phase` or `source_types`

**File:** `grimoire/mcp/tools.py:528`

The tool function's docstring describes the old Sigma-only interface and does not mention the `phase` or `source_types` parameters added in this PR.

---

## P6 — Technical Decisions / Open Questions

### T6.1 — `log_sources` in ChromaDB metadata (DECIDED — folded into T6.2)

**Decision: Add `log_sources` to `to_chromadb_metadata()` as a pipe-joined string, matching the approach used for `platforms`.**

This is implemented as part of T6.2's changes below.

---

### T6.2 — `platforms` / `log_sources`: JSONB arrays + GIN index (DECIDED)

**Decision: JSON arrays in PostgreSQL JSONB with `@>` / GIN index for SQL filtering; pipe-joined strings in ChromaDB for exact-match `$in` filtering. Add `log_sources` to ChromaDB metadata alongside `platforms`.**

**Rationale:** ILIKE on a pipe-joined string is an O(n) full-column scan at millions of rows. PostgreSQL's JSONB `@>` containment operator against a GIN-indexed column is O(log n). The ChromaDB path (pipe-joined strings for exact `$in` lookups) is O(1) hash lookup and should be kept — it works correctly for vector-layer filtering. The two representations serve different purposes and are not unified at the storage layer.

**Specific changes:**

1. **PostgreSQL filtering** — Replace `_jsonb_list_contains` ILIKE with JSONB `@>` / `func.jsonb_extract_path` + GIN index:
   ```python
   # New helper — uses GIN-indexable JSONB operators
   def _jsonb_array_contains(column: Any, key: str, value: str) -> Any:
       from sqlalchemy import func, String
       # Extract the array field and check membership using JSONB operators
       return func.jsonb_extract_path(column, key).cast(String).in_([value])
   ```
   Or using `@>` containment with a GIN index on `security_metadata`:
   ```python
   return column @> func.jsonb_build_object(key, [value])
   ```

2. **Add GIN index** — new Alembic migration:
   ```python
   op.create_index(
       "ix_documents_security_metadata_gin",
       "documents",
       ["security_metadata"],
       postgresql_using="gin",
   )
   ```

3. **ChromaDB metadata** — Add `log_sources` to `to_chromadb_metadata()` as pipe-joined string (unchanged `|` join, `$in` exact-match filter). Also add `log_sources` to the payload dict alongside `platforms`.

4. **Unified model** — List fields (`platforms`, `log_sources`, `cwe_ids`, `threat_actors`, `malware_families`, `ioc_types`, `detection_categories`) remain as JSON arrays in PostgreSQL JSONB. `to_chromadb_metadata()` pipe-joins for ChromaDB only. SQL-side filtering uses JSONB operators. No dual-format maintenance hazard since the conversion only happens at read-time for ChromaDB, not in PostgreSQL storage.

**Files to change:**
- `grimoire/mcp/tools.py` — replace `_jsonb_list_contains` with `_jsonb_array_contains`, update all call sites for `platform`, `log_source`, `phase`; also add `log_sources` to the ChromaDB filter dict in `_facet_search`
- `alembic/versions/` — add migration for GIN index on `security_metadata`
- `grimoire/strategies/security/metadata.py` — add `log_sources` to `to_chromadb_metadata()` as pipe-joined string, add comment documenting the JSONB/ChromaDB dual representation

---

### T6.3 — `source_types` API shape (DECIDED)

**Decision: Change `source_types` from `Literal["all", "playbooks", "sigma"]` to `List[str]` for future flexibility.**

Accept any list of source type strings (e.g., `["playbook", "sigma_rule"]`). Remove the validator that restricts to the three fixed values. The caller is responsible for passing valid source types (validated at the SQL layer). Keep `"all"` as a convenience shorthand — the caller or wrapper translates it to the full list before passing to `_facet_search`.

```python
# New shape
source_types: List[str] = Field(
    default=["playbook", "sigma_rule"],
    description="List of source types to search. E.g. ['playbook', 'sigma_rule'] or ['sigma_rule']. Defaults to both corpora.",
)
```

**Files to change:**
- `grimoire/mcp/tools.py` — `PlaybookSearchInput.source_types` → `List[str]`, remove `_validate_source_types` validator, update `grimoire_search_playbook` to accept list directly

---

### T6.4 — Facet-only playbook search (DECIDED)

**Decision: Implement a vector-bypass path for structured-facet-only searches (Option B).**

When `query` is absent (i.e., only structured facets like `mitre_technique_id`, `phase`, `platform`, `severity` are provided), bypass the vector search entirely and return pre-filtered document IDs directly from PostgreSQL as ranked results. This avoids depending on the query agent's handling of `"*"` and is more efficient for purely-structured queries.

**Implementation:**
1. In `grimoire_search_playbook`, detect when `params.query` is absent.
2. Build the SQL pre-filter conditions as currently done.
3. Instead of calling `_facet_search`, call `_matching_document_ids` directly to get the ranked list of matching document IDs.
4. Return those IDs + metadata as results without invoking the vector layer.
5. The response should indicate `"mode": "facet_only"` so clients know vector scoring was not applied.

**Files to change:**
- `grimoire/mcp/tools.py` — add facet-only branch in `grimoire_search_playbook`; add `"mode": "facet_only"` to response

---

### T6.5 — Remove unused `facet_label` from `_facet_search` (DECIDED)

**Decision: Remove `facet_label` from the `_facet_search` function signature.**

The parameter is passed from both call sites (`grimoire_search_cve` and `grimoire_search_playbook`) but is never used in the function body or response. Simplify the signature by removing it.

**Files to change:**
- `grimoire/mcp/tools.py` — remove `facet_label` parameter from `_facet_search` definition and all call sites

---

## Summary of Recommended Fixes (Prioritized)

| Priority | Issue | Fix |
|----------|-------|-----|
| P1.3 | `doc.chunks` accessed after delete | Collect vector IDs before delete |
| P1.2 | Wildcard `*` query may not work | Add explicit short-circuit for `query=="*"` or document limitation |
| P1.1 | JSONB ILIKE pattern fragility | Add comment documenting scalar-string assumption; consider JSONB operators |
| P2.4 | Stale "Phase 5" NotImplementedError doc | Remove from docstring |
| P2.3 | `matched_documents` name misleading | Rename to `sql_prefiltered_documents` |
| P3.3 | `facet_label` unused parameter | Remove from `_facet_search` signature |
| P2.1 | `log_sources` not in ChromaDB metadata | Add to `to_chromadb_metadata()` as pipe-joined string |
| P2.2 | `platform` SQL filter relies on fragile ILIKE | Add `platforms` to ChromaDB metadata; document SQL-only nature |
| P5.1 | `PlaybookSearchInput` docstring outdated | Update to describe playbooks + sigma corpora |
| P5.2 | Tool docstring missing `phase`/`source_types` | Update tool docstring |
| P4.1 | Missing sigma-only equality filter test | Add explicit test |
| P4.2 | Missing empty-result playbook search test | Add short-circuit test |
| P4.3 | Missing end-to-end playbook corpus test | Add e2e pipeline test |
| P4.4 | `pg_query` inner LIMIT handling | Strip inner LIMIT or document outer-LIMIT precedence |
| P3.1 | `log_source` vs `log_sources` naming | Standardise; prefer adding to ChromaDB metadata |
| P3.2 | `filter_dict` has no schema enforcement | Document expected keys or add validation |
| T6.1 | `log_sources` ChromaDB inclusion | Add to `to_chromadb_metadata()` |
| T6.2 | JSON array vs pipe-join dual format | Standardise; prefer JSON arrays + JSONB operators |
| T6.3 | `source_types` API shape | Keep as-is for v1 |
| T6.4 | Wildcard query fallback | Ensure query agent handles `*` or add vector-bypass path |
| T6.5 | Remove `facet_label` | Remove from function signature |
