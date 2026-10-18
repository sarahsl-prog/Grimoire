# Grimoire Code Review Report

**Date:** 2026-04-30  
**Branch:** pre-branch-testing  
**Scope:** /home/sunds/Code/Grimoire  

---

## Executive Summary

Overall the codebase is well-structured with good separation of concerns, Pydantic v2 models, proper async patterns, and comprehensive test coverage for the API layer. However, there are several **logic errors**, **missing error handling**, and **security concerns** that should be addressed before production use.

---

## 🔴 Critical Issues (Fix Before Production)

### 1. **Document Deletion Does Not Clean Up Vector Store** (`documents.py:99-112`)

**Issue:** When a document is deleted via the API, the code deletes the database row but the vectors remain in ChromaDB/Qdrant.

```python
@router.delete("/{document_id}", status_code=204)
async def delete_document(...) -> None:
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, ...)
    await db.delete(doc)  # ❌ Only deletes DB row
    await db.commit()      # ❌ Vectors orphaned in vector store
```

**Fix:** Add vector store cleanup before database deletion:
```python
# Get vector IDs from chunks before deletion
for chunk in doc.chunks:
    if chunk.vector_id:
        await vector_store.delete(chunk.vector_id)
await db.delete(doc)
```

**Risk:** Orphaned vectors accumulate over time, wasting storage and potentially polluting search results.

---

### 2. **Missing Cascade Delete for Category** (`categories.py:91-104`)

**Issue:** Deleting a category that has tagged documents will fail with a foreign key constraint error because `DocumentTag` has `cascade="all, delete-orphan"` on the category relationship, but documents still reference those tags.

**Fix:** Either:
- Remove tags before deleting category
- Or document the constraint and return 409 Conflict instead of 500

---

### 3. **Rate Limit Redis Configuration Bug** (`rate_limit.py:53-59`)

**Issue:** Redis URL construction has a bug - it references `settings.redis.host` twice instead of host and port:

```python
redis_url = (
    f"redis://{settings.redis.host}:{settings.redis.port}@{settings.redis.host}:{settings.redis.port}/{settings.redis.db_rate_limit}"
)
```

This generates: `redis://localhost:6379@localhost:6379/0` which is invalid.

**Fix:** Remove the duplicate host reference in the auth section.

---

### 4. **Ingestion Agent Called Without Await in Dependencies** (`dependencies.py:18-22`)

**Issue:** `get_ingestion_agent()` (and other agent getters) call build functions directly rather than async versions. If these build functions do I/O, they'll block the event loop.

```python
def get_ingestion_agent() -> Any:
    from grimoire.cli.helpers import build_ingestion_agent
    return build_ingestion_agent()  # ❌ Blocks if this does I/O
```

**Fix:** Make these async or use `functools.lru_cache` for singleton pattern.

---

### 5. **Category Slug Generation Not Unique** (`categories.py:59`)

```python
slug = body.name.lower().replace(" ", "-")  # ❌ No uniqueness check
```

**Fix:** Check for existing slug and append counter if needed, or return 409 if exists.

---

## 🟡 Medium Priority Issues

### 6. **No Transaction Rollback on API Errors**

Several routes commit without proper error handling:
- `categories.py:79` - `await db.commit()` with no try/except
- `documents.py:111` - Same issue
- `ingest.py:31,44` - No transaction boundary

**Fix:** Wrap mutations in try/except and rollback on error:
```python
try:
    await db.commit()
except Exception:
    await db.rollback()
    raise
```

---

### 7. **Missing Input Validation**

- `ingest.py` - `file_path` and `directory` not validated for path traversal (`../../../etc/passwd`)
- `categories.py:59` - No validation that `name` is URL-safe or doesn't contain special characters
- `generate.py:30` - `ContentType` conversion should be case-insensitive

---

### 8. **Health Check Too Simple** (`main.py:67-69`)

```python
@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}  # ❌ Doesn't check DB, Redis, or vector store
```

**Fix:** Add dependency checks:
```python
@app.get("/health")
async def health_check(db: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    checks = {"db": False, "redis": False, "vector_store": False}
    try:
        await db.execute(select(1))
        checks["db"] = True
    except Exception:
        pass
    # Check Redis and vector store similarly
    healthy = all(checks.values())
    return {"status": "healthy" if healthy else "degraded", "checks": checks}
```

---

### 9. **Settings Validation Bug** (`settings.py:211-213`)

The `validate_db_url` validator is cut off mid-implementation and duplicated:

```python
@field_validator("url", "dev_url")
@classmethod
def validate_db_url(cls, v: str) -> str:
    """Validate database URL format."""
    if not v.startswith(("postgresql://", "postgresql+asyncpg:***@field_validator("url")  # ❌ Cut off
@classmethod
def validate_postgres_url(cls, v: str) -> str:
```

This appears to be file corruption or bad merge. Needs cleanup.

---

### 10. **Version Mismatch** (`main.py:33` vs `pyproject.toml:3`)

```python
# main.py
app = FastAPI(version="0.1.0")  # ❌ Old version

# pyproject.toml
version = "2.0.0"  # ✅ Actual version
```

---

## 🟢 Low Priority / Code Quality

### 11. **Unused Import in main.py**

```python
from grimoire.api.routes import categories, documents, generate, ingest, query, watch
# ❌ api_keys is imported separately but others are already imported
```

Also `api_keys_router` is imported separately at line 63 when it could be in the group import.

---

### 12. **Inconsistent Enum Value Access**

In `documents.py:58-61`, there's a workaround for enum values:
```python
file_type=doc.file_type.value if hasattr(doc.file_type, "value") else str(doc.file_type),
```

This suggests the model might return strings sometimes. Should be consistent - always store enum values and convert to strings in Pydantic response models.

---

### 13. **Missing Pagination in Categories List** (`categories.py:23-48`)

List doesn't support offset/limit like documents does. Could be problematic with large category trees.

---

### 14. **No API Key Expiration Warning**

The `authenticate_api_key()` function silently returns `None` for expired keys. Should probably return a specific message indicating expiration vs invalid key.

---

### 15. **Missing Documentation Files**

`pyproject.toml` references:
```toml
Documentation = "https://github.com/sarahsl-prog/Grimoire/blob/main/docs/DESIGN.md"
```

But only `CODE_REVIEW_REPORT.md` exists in docs/. `DESIGN.md` is missing.

---

## 🔧 Test Coverage Gaps

From `tests/test_api.py`, the following scenarios are **not tested**:

1. **Authentication failure** - No tests for missing/invalid API keys
2. **Rate limiting** - No tests for rate limit enforcement
3. **Document deletion success** - Only "not found" is tested
4. **Category creation with parent_slug** - Only "not found" for parent is tested
5. **Ingest error handling** - Only success paths mocked
6. **Database connection failures** - No graceful degradation tests
7. **Vector store failures** - No tests for ChromaDB/Qdrant being down
8. **Concurrent modification** - No tests for version conflicts (Document.version exists but unused)

---

## 📋 Recommended Fixes (Priority Order)

| Priority | File | Issue | Fix |
|----------|------|-------|-----|
| 🔴 P0 | `documents.py` | Vector cleanup on delete | Add pre-delete hook to clear vectors |
| 🔴 P0 | `rate_limit.py` | Redis URL bug | Fix duplicate host reference |
| 🔴 P0 | `categories.py` | Slug uniqueness | Add collision check or unique constraint |
| 🟡 P1 | `main.py` | Health check | Add dependency health checks |
| 🟡 P1 | `ingest.py` | Path traversal | Validate/normalize paths |
| 🟡 P1 | `settings.py` | Validator corruption | Fix cut-off validator code |
| 🟢 P2 | `main.py` | Version mismatch | Update version to 2.0.0 |
| 🟢 P2 | - | Documentation | Add DESIGN.md or update pyproject.toml URL |

---

## 🚀 Security Recommendations

1. **Add request size limits** - FastAPI has no default upload size limit
2. **Sanitize file paths** - Prevent path traversal attacks
3. **Rate limiting on auth endpoints** - Currently only API routes are rate limited
4. **Add API key rotation** - No mechanism to rotate keys without creating new ones
5. **Audit logging** - No tracking of admin actions (key creation, deletion)

---

## 📝 Architectural Notes

### Good patterns found:
- Proper use of `asynccontextmanager` for lifespan management
- Clean dependency injection with FastAPI `Depends()`
- Good test isolation with dependency overrides
- Proper enum usage for type safety
- `ondelete="CASCADE"` on foreign keys

### Questions:
- Why does `Document` have a `version` field that's not used for optimistic locking?
- Is `ApiKeyTier` actually enforced anywhere beyond rate limiting?
- What's the purpose of `_get_watcher` global in watch routes?

---

Report generated by code review of Grimoire repository.
