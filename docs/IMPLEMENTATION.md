# Grimoire Implementation Plan
## Phased Development Roadmap

**Document:** IMPLEMENTATION.md  
**Status:** Draft  
**Last Updated:** 2026-03-29

---

## Overview

This document breaks the Grimoire redesign into **6 phases**, each with specific deliverables, dependencies, and agent task definitions. Each phase can be parallelized internally where dependencies don't block.

**Goal:** Build incrementally, validate each layer, ship working code at each phase.

---

## Phase 1: Foundation (Week 1-2)
**Goal:** Project skeleton, configuration, and base abstractions

### Deliverables
- [ ] Project structure with `pyproject.toml`
- [ ] Configuration system (Pydantic Settings)
- [ ] Database models and migrations (Alembic)
- [ ] Abstract base classes (ABCs) for all major components
- [ ] Logging setup with loguru
- [ ] Docker Compose for local dev (PostgreSQL, Redis, ChromaDB)

### Agent Tasks

#### Task 1.1: Project Skeleton
**Priority:** Blocker for everything
**Files to create:**
```
pyproject.toml          # With uv, ruff, black, mypy config
.gitignore
.env.example
README.md (minimal)
docker-compose.yml      # postgres:16, redis:7, chromadb
```
**Requirements:**
- Python 3.12+ required
- All tools configured per DESIGN.md Section 2
- uv commands work: `uv sync`, `uv run python --version`

**Validation:**
- [ ] `uv sync` succeeds
- [ ] `docker-compose up -d` starts all services
- [ ] `uv run python -c "import grimoire"` works
- [ ] **Documentation:** Update README.md with setup instructions

---

#### Task 1.2: Configuration System
**Priority:** Blocker for database setup
**Files to create:**
```
grimoire/config/__init__.py
grimoire/config/settings.py   # Pydantic Settings
```
**Requirements:**
- Load from `.env` and `grimoire.yaml`
- All settings from DESIGN.md Section 10
- Types: `GrimoireSettings` with nested models
- Validation: URLs, paths, enum values
- Override support: CLI flags override config

**Validation:**
- [ ] Unit tests: load settings from file, env, override
- [ ] Unit tests: invalid URLs rejected, invalid paths caught
- [ ] Unit tests: missing required fields raise clear errors
- [ ] Invalid config raises Pydantic ValidationError
- [ ] **Documentation:** Update DESIGN.md Section 10 if new config options added
- [ ] **Documentation:** Update .env.example with all required variables

---

#### Task 1.3: Database Models & Migrations
**Priority:** Blocker for core services
**Files to create:**
```
grimoire/db/__init__.py
grimoire/db/base.py         # SQLAlchemy Base, async session
grimoire/db/models.py       # All ORM models
grimoire/db/session.py      # Session management
alembic.ini
alembic/env.py              # Alembic configuration
alembic/versions/           # Initial migration
```
**Requirements:**
- All tables from DESIGN.md Section 3:
  - `documents`, `chunks`, `categories`
  - `document_tags`, `generated_content`
  - `relationships`, `watch_paths`, `processing_log`
  - `cache_entries`
- Relationships: Foreign keys, cascades
- Indexes: On `file_hash`, `source_path`, `created_at`
- UUID primary keys (uuid_generate_v4)
- Async SQLAlchemy: `create_async_engine`, `AsyncSession`

**Validation:**
- [ ] `alembic upgrade head` creates all tables
- [ ] `alembic downgrade` removes them
- [ ] Unit tests: CRUD on each model
- [ ] Unit tests: foreign key constraints enforced
- [ ] Unit tests: unique constraints (file_hash) work
- [ ] **Documentation:** Update DESIGN.md Section 3 if model changes

---

#### Task 1.4: Abstract Base Classes
**Priority:** Enables parallel Phase 2 work
**Files to create:**
```
grimoire/vectorstore/base.py
grimoire/storage/base.py
grimoire/core/cache.py      # Cache ABC
grimoire/utils/logger.py    # loguru setup
```
**Requirements:**
- `VectorStore` ABC: `initialize`, `add`, `search`, `delete`, `get`, `count`
- `StorageAdapter` ABC: `list_files`, `read`, `get_metadata`, `exists`, `list_changes` (cloud), `supports_watch`, `watch` (local)
- `Reranker` ABC: `rerank(query, documents, top_k)`
- `Cache` ABC: `get(key)`, `set(key, value, ttl)`, `delete(key)`, `clear()`
- Logger: Structured JSON, rotation, log directory creation

**Validation:**
- [ ] Abstract methods raise NotImplementedError
- [ ] Type hints on all methods

---

### Phase 1 Exit Criteria
- [ ] `uv run pytest grimoire/config/ grimoire/db/` passes
- [ ] `docker-compose up` brings up working dev stack
- [ ] Config loads from file and environment
- [ ] Database migrations run cleanly

---

## Phase 2: Core Services (Week 2-3)
**Goal:** Document processing pipeline components

### Deliverables
- [ ] Document parser (Docling wrapper)
- [ ] Chunking strategies (semantic, markdown, recursive)
- [ ] Embedding service
- [ ] Deduplication
- [ ] Tagger
- [ ] Full-text search (PostgreSQL FTS)
- [ ] Cache layer (Redis/DiskCache)

### Agent Tasks

#### Task 2.1: Document Parser
**Depends on:** Task 1.4 (no DB needed yet)
**Files to create:**
```
grimoire/core/parser.py
```
**Requirements:**
- Wrap Docling for PDF, DOCX, PPTX, XLSX, HTML, images
- Methods: `parse(file_path) -> ParsedDocument`
- Return: text, metadata (title, author, pages), images (optional)
- Error handling: Corrupt files log warning, don't crash
- Configurable: OCR enabled/disabled, parse images

**Validation:**
- [ ] Unit tests: parse sample PDF, DOCX, XLSX
- [ ] Test error case: corrupt PDF handled gracefully

---

#### Task 2.2: Chunking Strategies
**Depends on:** Task 1.1 (config loaded)
**Files to create:**
```
grimoire/core/chunker/
  __init__.py
  base.py              # Chunker ABC
  semantic.py          # SemanticChunker
  markdown.py          # MarkdownHeaderTextSplitter
  recursive.py         # RecursiveCharacterTextSplitter
```
**Requirements:**
- Each implements `chunk(text: str, strategy_config) -> List[Chunk]`
- Chunk object: content, token_count, index, prev/next links
- Semantic: embedding-based boundary detection
- Markdown: respect header hierarchy
- Recursive: configurable separators

**Validation:**
- [ ] Unit tests: each strategy produces valid chunks
- [ ] Test: overlapping chunks maintain continuity links

---

#### Task 2.3: Embedding Service
**Depends on:** Task 1.1 (config loaded)
**Files to create:**
```
grimoire/core/embedder.py
```
**Requirements:**
- Load model from config: `sentence-transformers/all-mpnet-base-v2`
- Method: `embed(texts: List[str]) -> List[List[float]]`
- Batch processing with progress logging
- GPU support if available
- Caching: Check Redis before computing

**Validation:**
- [ ] Unit tests: embed returns correct dims
- [ ] Test batch processing
- [ ] Test cache hit/miss

---

#### Task 2.4: Deduplication
**Depends on:** Task 1.2 (settings)
**Files to create:**
```
grimoire/core/dedup.py
```
**Requirements:**
- Compute SHA-256 of file content
- Check against `documents.file_hash`
- Return: `skip`, `update`, `new`
- Handle version conflicts per DESIGN.md Section 8.4

**Validation:**
- [ ] Unit tests: same file hash returns skip
- [ ] Unit tests: modified file returns update

---

#### Task 2.5: Tagger (LLM Auto-Tagging)
**Depends on:** Task 1.3 (DB for categories)
**Files to create:**
```
grimoire/core/tagger.py
```
**Requirements:**
- Method: `suggest_tags(document_sample, categories) -> List[(tag, confidence)]`
- Uses Ollama LLM from config
- Confidence thresholding
- Store suggestions in `document_tags` with confidence and source=llm

**Validation:**
- [ ] Unit test with mocked LLM
- [ ] Integration test with real Ollama

---

#### Task 2.6: Full-Text Search
**Depends on:** Task 1.3 (DB), Task 1.2 (settings)
**Files to create:**
```
grimoire/search/fulltext.py
```
**Requirements:**
- PostgreSQL FTS: `to_tsvector`, `to_tsquery`
- Create GIN index on `chunks.content`
- Method: `search(query: str, top_k: int) -> List[ChunkResult]`
- Weighted fields: title higher than body

**Validation:**
- [ ] Unit tests: search returns ranked results
- [ ] Test with sample chunks in test DB

---

#### Task 2.7: Cache Layer
**Depends on:** Task 1.4 (Cache ABC), Task 1.2 (Redis config)
**Files to create:**
```
grimoire/core/cache.py       # Redis/DiskCache implementation
```
**Requirements:**
- Implement `Cache` ABC with Redis backend
- TTL support, key namespacing
- Serialization: JSON

**Validation:**
- [ ] Unit tests: set/get/delete/ttl
- [ ] Test Redis connection error handling

---

### Phase 2 Exit Criteria
- [ ] `uv run pytest grimoire/core/ grimoire/search/` passes
- [ ] Can parse, chunk, embed a test document end-to-end
- [ ] Cache and FTS working

---

## Phase 3: Storage Adapters & Vector Store (Week 3-4)
**Goal:** Abstract storage layer

### Deliverables
- [ ] Vector store: ChromaDB implementation
- [ ] Storage adapters: Local, USB, Google Drive, OneDrive
- [ ] Watch manager (hybrid polling)

### Agent Tasks

#### Task 3.1: ChromaDB Vector Store
**Depends on:** Task 1.4 (VectorStore ABC), Task 2.3 (embedder returns dims)
**Files to create:**
```
grimoire/vectorstore/chromadb.py
```
**Requirements:**
- Implements `VectorStore` ABC
- Initialize collection with metadata schema
- Store with: id, embedding, metadata (doc_id, chunk_idx, tags)
- Search with filter support: `{"tags": {"$contains": "research"}}`

**Validation:**
- [ ] Unit tests: add, search, filter, delete
- [ ] Test metadata filtering

---

#### Task 3.2: Local Storage Adapter
**Depends on:** Task 1.4 (StorageAdapter ABC)
**Files to create:**
```
grimoire/storage/local.py
```
**Requirements:**
- Implements `StorageAdapter`
- `list_files`: Walk directory, return `FileInfo` objects
- `read_file`: Open binary, return bytes
- `watch`: Return `watchdog.Observer`

**Validation:**
- [ ] Unit tests: list files, read file, get metadata
- [ ] Test with temp directory

---

#### Task 3.3: Watch Manager (Hybrid)
**Depends on:** Task 3.2 (Local adapter)
**Files to create:**
```
grimoire/storage/watch_manager.py
```
**Requirements:**
- `WatchManager`: manages multiple watchers
- Local paths: use `watchdog`
- Cloud paths: polling with `list_changes(since)`
- Configurable poll intervals per cloud backend

**Validation:**
- [ ] Unit tests: start/stop watchers
- [ ] Test event handling callback

---

#### Task 3.4: Google Drive Adapter
**Depends on:** Task 1.4, Task 1.2 (Google Drive credentials)
**Files to create:**
```
grimoire/storage/gdrive.py
```
**Requirements:**
- OAuth2 flow for authentication
- `list_files`: `files.list` API
- `list_changes`: `changes.list` with page tokens

**Validation:**
- [ ] Manual test: list files from real Drive
- [ ] Unit tests with mocked API

---

#### Task 3.5: OneDrive Adapter
**Depends on:** Task 1.4, Task 1.2 (OneDrive credentials)
**Files to create:**
```
grimoire/storage/onedrive.py
```
**Requirements:**
- Microsoft Graph API OAuth
- `list_files`: delta endpoint
- `list_changes`: same endpoint with delta token

**Validation:**
- [ ] Manual test: list files from real OneDrive
- [ ] Unit tests with mocked API

---

### Phase 3 Exit Criteria
- [ ] Vector search with filters works
- [ ] Can read files from local, GDrive, OneDrive
- [ ] Watch manager handles mixed local/cloud paths

---

## Phase 4: Agents (Week 4-5)
**Goal:** Agent logic with Deep Agents
**Status:** Complete (2026-04-01)

### Deliverables
- [x] Ingestion Agent
- [x] Watcher Agent
- [x] Query Agent
- [x] Content Generation Agent

### Agent Tasks

#### Task 4.1: Ingestion Agent
**Depends on:** Tasks 2.1-2.5, 3.1-3.2 (pipeline components)
**Files to create:**
```
grimoire/agents/ingestion.py
```
**Requirements:**
- LangChain Deep Agent with tools:
  - `detect_file_type`, `check_dedup`
  - `extract_content`, `chunk_document`
  - `embed_chunks`, `store_vectors`
  - `auto_tag`, `log_processing`
- Orchestrates full pipeline in order
- Error handling: Log failures, continue with next file

**Validation:**
- [x] Integration test: ingest sample directory
- [x] Test error recovery

---

#### Task 4.2: Watcher Agent
**Depends on:** Task 3.3 (Watch Manager)
**Files to create:**
```
grimoire/agents/watcher.py
```
**Requirements:**
- Long-running daemon
- On file event: spawn Ingestion Agent for that file
- Handle multiple watches concurrently

**Validation:**
- [x] Unit test: file created triggers ingestion

---

#### Task 4.3: Query Agent
**Depends on:** Task 3.1 (vector search), Task 2.6 (FTS), Task 2.3 (embedder)
**Files to create:**
```
grimoire/agents/query.py
grimoire/search/hybrid.py    # Combines vector + FTS
```
**Requirements:**
- Agentic RAG pipeline:
  1. Vector search (top 50)
  2. Optional: FTS search (top 20)
  3. Merge + deduplicate
  4. Rerank (cross-encoder)
  5. Generate answer with citations
- Tools: `vector_search`, `full_text_search`, `hybrid_search`
- Cache results

**Validation:**
- [x] Integration test: query returns relevant chunks
- [x] Test citations include source doc IDs

---

#### Task 4.4: Content Generation Agent
**Depends on:** Task 1.3 (generated_content table), Task 4.3 (query for context)
**Files to create:**
```
grimoire/agents/content_gen.py
```
**Requirements:**
- Tools: `generate_summary`, `generate_flashcards`, etc.
- Cache generated content
- Store in `generated_content` table

**Validation:**
- [x] Unit tests: each generation type
- [x] Test caching

---

### Phase 4 Exit Criteria
- [x] Can ingest documents via agent
- [x] Can query and get answers
- [x] Can generate summaries

---

## Phase 5: CLI & API (Week 5-6)
**Goal:** User-facing interfaces

### Deliverables
- [x] CLI commands (Click)
- [x] FastAPI REST API
- [x] Integration between CLI/API and agents

### Agent Tasks

#### Task 5.1: CLI Implementation
**Depends on:** Tasks 4.1-4.4 (agents callable)
**Files to create:**
```
grimoire/cli/main.py
grimoire/cli/ingest.py
grimoire/cli/watch.py
grimoire/cli/query.py
grimoire/cli/generate.py
grimoire/cli/categories.py
grimoire/cli/config.py
grimoire/cli/status.py
```
**Requirements:**
- All commands from DESIGN.md Section 12
- Async Click commands: `@click.command() -> async def`
- Rich output: progress bars, tables

**Validation:**
- [x] CLI smoke tests for each command

---

#### Task 5.2: FastAPI REST API
**Depends on:** Tasks 4.1-4.4
**Files to create:**
```
grimoire/api/main.py
grimoire/api/routes/ingest.py
grimoire/api/routes/query.py
grimoire/api/routes/documents.py
grimoire/api/routes/categories.py
grimoire/api/routes/watch.py
grimoire/api/schemas.py
grimoire/api/dependencies.py
```
**Requirements:**
- All routes from DESIGN.md Section 8
- Pydantic schemas
- Background tasks for long operations (ingest)
- Auto-generated OpenAPI docs

**Validation:**
- [x] API tests with httpx/httpx.AsyncClient

---

### Phase 5 Exit Criteria
- [x] CLI commands work end-to-end
- [x] API starts and responds
- [x] OpenAPI docs accessible at `/docs`

---

## Phase 6: Integration & Polish (Week 6-7)
**Goal:** Testing, docs, migration

### Deliverables
- [ ] Full test suite
- [ ] Documentation updates
- [ ] Migration script from legacy Grimoire
- [ ] Performance testing

### Agent Tasks

#### Task 6.1: Test Suite
**Files to create:**
```
tests/
  conftest.py            # Fixtures, test DB setup
  unit/                  # Unit tests per module
  integration/           # End-to-end tests
```
**Requirements:**
- >80% coverage
- Integration tests: Full ingest → query pipeline

---

#### Task 6.2: Migration Script
**Files to create:**
```
grimoire/migrate_legacy.py
```
**Requirements:**
- Export from legacy FAISS store
- Import to new ChromaDB + PostgreSQL
- Re-chunk with new strategies

---

## Appendix A: Agent Task Template

When spawning an agent, use this template:

```
TASK: [Task X.Y: Name]

GOAL: [What to implement]

CONTEXT:
- Repository: https://github.com/sarahsl-prog/Grimoire
- Branch: grf/design-doc-update (or create feature branch)
- Read: docs/DESIGN.md Section [relevant]
- Read: docs/IMPLEMENTATION.md Task X.Y
- Read: Claude.md for coding conventions

DEPENDENCIES:
- Must complete: [list blockers before starting]
- Can work in parallel with: [list]

DELIVERABLES:
1. [File path] - [Description]
2. [File path] - [Description]

CONVENTIONS (REQUIRED):
- Use uv for all package operations
- Type hints everywhere, mypy --strict
- Black 88 chars: uv run black app tests
- Async first for I/O
- loguru for logging
- **Tests: pytest with >80% coverage, including edge cases and input validation**
- **Documentation: Update relevant docs/*.md files when changing architecture**

VALIDATION:
- [ ] uv run pytest [relevant tests] passes
- [ ] uv run ruff check . has no errors
- [ ] uv run black app tests formats cleanly
- [ ] mypy --strict passes
- [ ] **Documentation updated (if architecture changed)**

COMMIT MESSAGE TEMPLATE:
feat(component): [what changed]

- [bullet points]
- Closes #[issue if applicable]
```

---

## Appendix B: Phase Checklist

Before starting phase N+1:

- [ ] All Phase N tasks complete
- [ ] All Phase N tests pass
- [ ] Code review approved
- [ ] Documentation updated
- [ ] Migration script (if breaking changes)

---

## Appendix C: Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Agent implements wrong pattern | Reference DESIGN.md ABCs explicitly in task |
| Dependencies unclear | List blockers explicitly in task |
| Code review too large | Small, reviewable commits per task |
| Testing gaps | Require coverage >80% before merge |
| Integration failures | Integration tests at end of each phase |

---

**End of Implementation Plan**

---

## Appendix D: Testing Standards & Requirements

### Required Test Categories

Every module MUST include tests for:

#### 1. Happy Path Tests
- Basic functionality works with valid inputs
- Standard use cases produce expected results

#### 2. Edge Cases & Boundary Conditions
- Empty inputs: `[]`, `""`, `{}`, `None`
- Single element inputs (minimum valid case)
- Maximum capacity tests (large files, many chunks)
- Unicode/special characters in text
- Long paths, weird filenames
- Missing optional fields in config

#### 3. Input Validation & Error Handling
- Invalid types (pass int where str expected)
- Out-of-range values (negative IDs, too-high top_k)
- Malformed data (corrupt file, invalid JSON)
- Schema violations (wrong Pydantic model shapes)
- SQL injection attempts (should fail parameterized queries)

#### 4. Async Behavior
- Concurrent access (multiple threads/processes)
- Race conditions
- Timeout handling
- Connection failures (Redis, DB, Ollama)

#### 5. State Management
- Re-entrant operations (idempotency)
- Partial failures (rollback behavior)
- Cache invalidation
- Duplicate handling

### Required Test File Pattern

```python
# test_component.py structure

class TestComponentHappyPath:
    """Standard use cases."""
    def test_basic_functionality(self):
        pass

class TestComponentEdgeCases:
    """Boundary conditions and unusual inputs."""
    def test_empty_input(self):
        pass
    
    def test_single_element(self):
        pass
    
    def test_maximum_capacity(self):
        pass

class TestComponentInputValidation:
    """Invalid inputs are rejected gracefully."""
    def test_invalid_type_raises_error(self):
        pass
    
    def test_out_of_range_value(self):
        pass
    
    def test_missing_required_field(self):
        pass

class TestComponentErrorHandling:
    """Errors are caught and handled appropriately."""
    def test_network_failure_recovery(self):
        pass
    
    def test_corrupt_file_handling(self):
        pass

class TestComponentConcurrency:
    """Async and concurrent behavior."""
    @pytest.mark.asyncio
    async def test_concurrent_access(self):
        pass
```

### Test Fixtures Requirements

- **Database**: Use `pytest-asyncio` with temp PostgreSQL via testcontainers or SQLite in-memory
- **Files**: Create temp directories with sample docs in `tests/fixtures/`
- **Config**: Override settings for tests (use `TestSettings`)
- **Mocks**: Use `unittest.mock` or `pytest-mock` for external APIs (Ollama, GDrive)

### Coverage Requirements

- Minimum **80%** line coverage
- **100%** coverage for:
  - Input validation functions
  - Error handling branches
  - Security-critical code (auth, SQL, file paths)

### Prohibited Test Practices

❌ **Do NOT:**
- Only test "happy path" - every function needs error cases
- Skip tests because "it's just a wrapper" - wrappers have failure modes
- Use sleeps/waits instead of proper async synchronization
- Test implementation details instead of behavior
- Skip edge cases "because that won't happen" - it will

---

## Appendix E: Documentation Requirements

### When to Update Documentation

**ALWAYS update docs when:**
- Adding new configuration options (update DESIGN.md Section 10)
- Changing ABC interfaces (update DESIGN.md Section 6, 7)
- Adding new CLI commands (update DESIGN.md Section 12)
- Modifying data models (update DESIGN.md Section 3)
- Breaking changes to existing functionality

**Code-level documentation:**
- All public functions have docstrings (Google style)
- Type hints on all public APIs
- README.md updated if setup changes
- .env.example updated if new env vars added

### Documentation Files

| File | Purpose | Update When |
|------|---------|-------------|
| `DESIGN.md` | Architecture, data models, conventions | Architecture changes |
| `IMPLEMENTATION.md` | This file - roadmap and tasks | Planning changes |
| `README.md` | Quick start, setup | Setup process changes |
| `.env.example` | Environment variables | New config options |
| `CHANGELOG.md` | Version history | Each release |

---

---
