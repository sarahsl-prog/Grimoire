# Grimoire Design Document
## Agent-Based Knowledge Management System v2.0

**Status:** Draft  
**Last Updated:** 2026-03-29  
**Authors:** sarahsl-prog (with Grf)

---

## Executive Summary

This document outlines the complete redesign of Grimoire from a minimal 2-file RAG system into a production-ready, modular, agent-based knowledge management platform. The system supports 100K+ documents, hierarchical auto-tagging, multi-source cloud storage, and on-demand content generation.

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| CLI + API first | Power users need automation; web UI comes later |
| 100K+ document scale | Requires Qdrant-ready architecture from day one |
| Local LLM via Ollama | Privacy-first, no API costs, offline capable |
| Pluggable cloud storage | rclone for mount flexibility, native APIs for efficiency |
| On-demand content generation | Storage savings, user-controlled quality |

---

## 1. System Architecture

### Four-Layer Architecture

```
┌─────────────────────────────────────────────────┐
│                   CLI / API Layer                │
│         (Click CLI + FastAPI REST API)           │
├─────────────────────────────────────────────────┤
│                  Agent Layer                     │
│   (LangChain Deep Agents - prebuilt tools,      │
│    automatic context compression, subagents)     │
├──────────┬──────────┬───────────┬───────────────┤
│ Ingestion│ Watcher  │ Query     │ Content Gen   │
│ Agent    │ Agent    │ Agent     │ Agent         │
├──────────┴──────────┴───────────┴───────────────┤
│                 Core Services                    │
│  Storage Adapters │ Document Parser │ Tagger     │
│  Chunker │ Embedder │ Metadata DB │ Vector Store │
│  Reranker │ Full-Text Search │ Cache Layer      │
├─────────────────────────────────────────────────┤
│              Storage / Persistence               │
│   PostgreSQL (metadata) │ ChromaDB/Qdrant (vec)  │
│   Local FS │ Hybrid Cloud Sync (watchdog + poll) │
└─────────────────────────────────────────────────┘
```

### Architecture Principles

1. **Separation of concerns:** Each layer has a single responsibility
2. **Pluggability:** All major components implement ABCs for easy swapping
3. **Event-driven:** Agents communicate via events/messages
4. **Local-first:** All processing possible without cloud dependencies
5. **Observability:** Full tracing and logging at every layer

---

## 2. Technology Stack

| Layer | Technology | Rationale |
|-------|------------|-----------|
| **CLI** | Click | Standard Python CLI framework, composable commands |
| **API** | FastAPI | Async, high-performance, auto-docs, background tasks |
| **Agent Orchestration** | **LangChain Deep Agents** | Batteries-included agent architecture, automatic context compression, virtual filesystem, subagent spawning |
| **LLM** | Ollama (any model) | Local-first, model-agnostic |
| **Embeddings** | sentence-transformers (configurable, default: all-mpnet-base-v2) | Higher quality than all-MiniLM, still efficient |
| **Vector Store** | ChromaDB (primary) / Qdrant-ready interface | ChromaDB: embedded, no server, native metadata filtering. **Abstract interface allows Qdrant swap for scale.** |
| **Metadata DB** | **PostgreSQL 16+** (primary) / SQLite (dev testing) | SQLAlchemy ORM; PostgreSQL for production FTS and scale |
| **Document Parser** | Docling | MIT license, 97.9% accuracy on complex tables, PDF/DOCX/PPTX/XLSX/images/audio/HTML, local execution |
| **File Watching** | watchdog (local) + polling (cloud) | **Hybrid approach**: events for local FS, polling for cloud APIs |
| **Full-Text Search** | **PostgreSQL FTS** (primary) / SQLite FTS (dev) | PG FTS with weighted fields, ranks, and pgvector hybrid support |
| **Caching** | **Redis** / DiskCache | Query embedding cache, result cache, task state |
| **Task Queue** | asyncio + background threads (MVP) / **Celery** (scale) | Celery with Redis broker for distributed processing |
| **Package Manager** | **uv** | Fast, modern Python package management; preferred over pip |
| **Logging** | **loguru** | Structured logging with rotation; output to `/log` or `/var/log/grimoire` |

### Vector Store: ChromaDB with Qdrant Migration Path

**ChromaDB Rationale:**
- Native metadata filtering (critical for tags/categories at 100K+ scale)
- Embedded mode (no separate server)
- Handles millions of vectors on single machine
- Apache 2.0 license

**Qdrant Upgrade Path:**
- Design uses `VectorStore` ABC with `ChromaDBStore` and `QdrantStore` implementations
- Same interface, swapable at configuration time
- Qdrant offers: 10x+ throughput, multi-vector per document, one-stage HNSW filtering

**Migration Triggers:**
- >500K vectors (chunked: ~100K documents)
- Need multi-tenancy
- Complex multi-field filtering requirements

### Project Conventions for Agents / Developers

All contributors and coding agents must follow these conventions:

**Package Management (Required):**
- Use `uv` for all dependency management
- Install: `uv add <package>`
- Remove: `uv remove <package>`
- Sync: `uv sync`
- Run tools: `uv run pytest`, `uv run ruff check .`, `uv run ruff format .`

**Code Style (Required):**
- Python 3.12+ with type hints everywhere; code must pass `mypy --strict`
- Format with `black` (88 char line length)
- Use f-strings, no wildcard imports
- Pydantic models for all external I/O, FastAPI `Depends` for DI
- Prefer async endpoints and non-blocking libraries
- Well-commented, modular code with docstrings

**Logging (Required):**
- Use `loguru` for all logging
- Log directory: `/log` during dev, `/var/log/grimoire` on Linux
- Never log secrets, tokens, or PII

**Security (Required):**
- Parameterized DB queries only (SQLAlchemy ORM)
- Validate all external inputs with Pydantic
- No secrets in code; use environment variables

**Testing (Required):**
- Use `pytest` for all tests
- Run `uv run pytest -q` for quick checks
- Run `uv run ruff check .` and `black` before committing
- High test coverage for public functions
- Test with `curl` or HTTP client for API changes

**Database Access:**
- PostgreSQL 16+ for production, SQLite only for dev testing
- Use async SQLAlchemy (`async_sessionmaker`)
- Alembic migrations for schema changes

---

## 3. Data Model

### PostgreSQL/SQLite Tables (via SQLAlchemy)

```python
# documents — Core document metadata
{
    "id": "uuid",
    "source_path": "string (URI format)",
    "storage_backend": "enum: local/usb/rclone/gdrive/onedrive",
    "file_type": "enum: pdf/docx/pptx/xlsx/html/md/txt/...",
    "file_hash": "string (SHA-256)",
    "title": "string (extracted or filename)",
    "size_bytes": "integer",
    "created_at": "timestamp (file)",
    "updated_at": "timestamp (file)",
    "processed_at": "timestamp (system)",
    "processing_status": "enum: pending/processing/completed/failed/stale",
    "error_message": "text",
    "version": "integer (conflict detection)",
    # Security metadata (Phase 2 of the security strategy plan).
    # All nullable; non-security ingest leaves these blank.
    "source_type": "string (e.g. 'nvd_cve', 'sigma_rule')",
    "cve_id": "string (e.g. 'CVE-2024-12345')",
    "severity": "enum: critical/high/medium/low/info/unknown",
    "mitre_technique_id": "string (e.g. 'T1059.001')",
    "mitre_tactic": "string (e.g. 'execution', 'persistence')",
    "tlp_level": "enum: white/green/amber/red",
    "content_date": "timestamp (effective date of underlying content)",
    "security_metadata": "JSONB (wide/sparse SecurityMetadata fields: cvss_score, cwe_ids, affected_products, threat_actors, malware_families, ioc_types, detection_categories, platforms, log_sources, published_date, mitre_subtechnique, source_url)"
}

# chunks — Document chunks with embeddings
{
    "id": "uuid",
    "document_id": "FK → documents",
    "chunk_index": "integer",
    "content": "text",
    "token_count": "integer",
    "vector_id": "string (ChromaDB reference)",
    "embedding_model": "string (which model generated)",
    "prev_chunk_id": "FK → chunks (continuity)",
    "next_chunk_id": "FK → chunks (continuity)",
    "created_at": "timestamp"
}

# categories — Hierarchical taxonomy
{
    "id": "uuid",
    "name": "string",
    "slug": "string (URL-safe)",
    "parent_id": "FK self-referential (null = root)",
    "description": "text",
    "color": "string (hex, for UI)",
    "created_at": "timestamp"
}

# document_tags — Many-to-many junction with confidence
{
    "document_id": "FK → documents",
    "category_id": "FK → categories",
    "confidence": "float (0.0-1.0)",
    "tagged_by": "enum: llm/user/rule",
    "created_at": "timestamp"
}

# generated_content — On-demand derived content
{
    "id": "uuid",
    "document_id": "FK → documents",
    "content_type": "enum: summary/flash_card/cliff_notes/outline/image/extract",
    "content": "text",
    "model_used": "string",
    "generation_params": "JSON (temperature, tokens, etc.)",
    "created_at": "timestamp",
    "cache_hit": "boolean"
}

# relationships — Document-to-document links
{
    "id": "uuid",
    "source_document_id": "FK → documents",
    "target_document_id": "FK → documents",
    "relationship_type": "enum: related/references/summarizes/derived_from/similar",
    "confidence": "float",
    "discovered_by": "enum: llm/user/manual",
    "created_at": "timestamp"
}

# watch_paths — Monitored directories
{
    "id": "uuid",
    "path": "string (URI format)",
    "storage_backend": "enum",
    "recursive": "boolean",
    "active": "boolean",
    "last_scanned_at": "timestamp",
    "poll_interval_seconds": "integer (cloud polling only)",
    "created_at": "timestamp"
}

# processing_log — Audit trail
{
    "id": "uuid",
    "document_id": "FK → documents",
    "action": "enum: discovered/extracted/chunked/tagged/failed",
    "status": "enum: success/partial/failed",
    "details": "JSON",
    "duration_ms": "integer",
    "created_at": "timestamp"
}

# cache_entries — Result caching
{
    "id": "uuid",
    "cache_key": "string (hash of query params)",
    "cache_type": "enum: query/search/generated",
    "data": "JSON",
    "expires_at": "timestamp",
    "hit_count": "integer",
    "created_at": "timestamp"
}

# wiki_pages — LLM-generated wiki pages
{
    "id": "uuid",
    "title": "string",
    "slug": "string (URL-safe, unique)",
    "content": "text",
    "version": "integer",
    "status": "enum: draft/compiled/flagged",
    "entity_type": "string (optional)",
    "created_at": "timestamp",
    "updated_at": "timestamp"
}

# wiki_page_sections — Sections within wiki pages
{
    "id": "uuid",
    "wiki_page_id": "FK → wiki_pages",
    "heading": "string",
    "content": "text",
    "section_index": "integer",
    "source_document_id": "FK → documents (nullable)",
    "source_priority": "integer",
    "contradiction_flag": "text (nullable)",
    "superseded_by_section_id": "FK self-referential (nullable)",
    "created_at": "timestamp",
    "updated_at": "timestamp"
}

# wiki_cross_references — Cross-reference links between wiki pages
{
    "id": "uuid",
    "source_page_id": "FK → wiki_pages",
    "target_page_id": "FK → wiki_pages",
    "ref_type": "enum: references/depends_on/related_to/contradicts",
    "context": "text (nullable)",
    "created_at": "timestamp"
}

# wiki_compile_jobs — Wiki compilation tracking
{
    "id": "uuid",
    "document_id": "FK → documents",
    "status": "enum: pending/compiling/completed/failed",
    "compiled_at": "timestamp (nullable)",
    "error_message": "text (nullable)",
    "created_at": "timestamp"
}
```

---

## 4. Processing Pipeline

### Enhanced Pipeline with Semantic Chunking

```
File Discovery (scan/watch/poll)
    │
    ▼
File Type Detection (python-magic + extension)
    │
    ▼
Deduplication Check (SHA-256 hash)
    │
    ▼
Content Extraction (Docling)
    │
    ▼
Chunking Strategy Selection
    ├─ Markdown files → MarkdownHeaderTextSplitter
    ├─ Code files → Recursive + language-specific
    └─ General docs → SemanticChunker (embedding-based)
    │
    ▼
Chunking (with overlap and continuity tracking)
    │
    ▼
Embedding (Configurable model)
    │
    ▼
Vector Storage (ChromaDB with metadata + FTS indexing)
    │
    ▼
Auto-Tagging (LLM categorizes against existing taxonomy)
    │
    ▼
Metadata Storage (SQLAlchemy)
    │
    ▼
Relationship Discovery (optional, on-demand)
```

### Chunking Strategies

| Strategy | Best For | Implementation |
|----------|----------|----------------|
| **SemanticChunker** | Mixed documents, respects semantic boundaries | Uses embeddings to split at low-similarity boundaries |
| **MarkdownHeaderTextSplitter** | .md files | Respects header hierarchy (# ## ###) |
| **RecursiveCharacterTextSplitter** | Code, structured text | Splits on separators, configurable overlap |
| **Language-aware splitters** | Python, JS, etc. | Respects function/class boundaries |

**Chunk Continuity:** Each chunk stores `prev_chunk_id` and `next_chunk_id` links for context restoration during retrieval.

---

## 5. Agent Design (LangChain Deep Agents)

**Design Change:** Using LangChain Deep Agents instead of LangGraph for the following advantages:
- Pre-built agent architecture (less boilerplate)
- Automatic compression of long conversations
- Virtual filesystem for tool isolation
- Native subagent spawning for context isolation

### Four Specialized Agents

#### Ingestion Agent
**Purpose:** File discovery and processing through the pipeline

**Tools:**
- `list_files(path, recursive)` — Storage adapter abstraction
- `detect_file_type(file_path)` — MIME type + extension
- `check_dedup(file_hash)` — Check if already processed
- `extract_content(file_path)` — Docling wrapper
- `chunk_document(content, strategy)` — Chunker with strategy selection
- `embed_chunks(chunks, model)` — HuggingFace embeddings
- `store_vectors(chunks_with_embeddings)` — Vector store interface
- `auto_tag(document, categories)` — LLM categorization
- `log_processing(document_id, action, status)` — Audit trail

**State:** IngestionState(tracing, current_file, progress)

#### Watcher Agent
**Purpose:** Monitor directories and trigger ingestion

**Architecture:**
- **Local paths:** watchdog Observer with custom event handler
- **Cloud paths:** Periodic polling via `list_changes(since)` API

**Tools:**
- `watch_directory(path, recursive, backend)` — Start monitoring
- `unwatch_directory(watch_id)` — Stop monitoring
- `poll_cloud(path, last_poll_time)` — Poll for changes
- `get_watch_status()` — List active watchers
- `handle_file_event(event)` — Route to Ingestion Agent

**Implementation Notes:**
- watchdog uses inotify on Linux, ReadDirectoryChanges on Windows
- Cloud adapters implement `list_changes(since: datetime)` instead of `watch()`
- Polling interval configurable per watch (default: 300s for cloud)

**State:** WatcherState(active_watches, event_queue, last_poll_times)

#### Query Agent
**Purpose:** User question answering with agentic RAG

**Enhanced Pipeline with Reranking:**
```
User Query
    │
    ▼
Query Expansion (optional: LLM rewrites for recall)
    │
    ▼
Vector Search + Metadata Filters → Top 50
    │
    ▼
Full-Text Search (complementary) → Top 20
    │
    ▼
Merge + Deduplicate
    │
    ▼
Cross-Encoder Reranking → Top 5
    │
    ▼
LLM Context Assembly
    │
    ▼
Answer Generation with Citations
```

**Tools:**
- `vector_search(query, filters, top_k)` — Semantic search
- `full_text_search(query, top_k)` — Exact match search
- `hybrid_search(query, filters)` — Combines both, configurable weights
- `rerank(query, documents, model)` — Cross-encoder reranking
- `filter_by_tags(tags, operator)` — AND/OR tag filtering
- `filter_by_metadata(field, op, value)` — Generic metadata filters
- `get_related_documents(doc_id)` — Graph traversal
- `get_document_details(doc_id)` — Full metadata + chunks
- `check_cache(query_hash)` — Result caching

**Reranking Models:**
- Default: `cross-encoder/ms-marco-MiniLM-L-6-v2` (small, fast)
- Upgrade: `BAAI/bge-reranker-base` (better accuracy)

**State:** QueryState(query, retrieved_docs, reranked_docs, answer, citations)

#### Content Generation Agent
**Purpose:** Create derived content on-demand

**Tools:**
- `generate_summary(doc_ids, style)` — Abstract/detailed
- `generate_flash_cards(doc_ids, count)` — Q&A pairs
- `generate_cliff_notes(doc_ids)` — Bullet summary
- `generate_outline(doc_ids)` — Hierarchical structure
- `generate_extract(doc_ids, query)` — Specific information
- `store_generated_content(doc_id, content, type)` — Save to DB
- `check_generation_cache(doc_ids, type)` — Avoid regeneration

**Cache Strategy:**
- Hash of `doc_ids + content_type + generation_params`
- TTL: 30 days default
- Invalidate on source document update

**State:** GenerationState(request, generated_content, model_used, cached)

### Coordinator
Deep Agents natively support subagent spawning, so the "coordinator" is implicit in the agent's ability to spawn child agents for isolated tasks.

---

## 6. Storage Adapter Interface

### Updated Abstract ABC

```python
from abc import ABC, abstractmethod
from typing import List, Optional, AsyncIterator
from datetime import datetime

class StorageAdapter(ABC):
    """Abstract base for all storage backends."""
    
    @abstractmethod
    async def list_files(self, path: str, recursive: bool = False) -> List[FileInfo]:
        """List files in a directory."""
        pass
    
    @abstractmethod
    async def read_file(self, path: str) -> bytes:
        """Read file contents."""
        pass
    
    @abstractmethod
    async def get_metadata(self, path: str) -> FileMetadata:
        """Get file metadata (size, mtime, hash if available)."""
        pass
    
    @abstractmethod
    async def exists(self, path: str) -> bool:
        """Check if file/directory exists."""
        pass
    
    # Cloud-specific methods
    @abstractmethod
    async def list_changes(self, since: datetime, path: str = None) -> List[FileChange]:
        """
        List changes since timestamp. Required for cloud adapters.
        Local adapters may raise NotImplementedError.
        """
        pass
    
    @abstractmethod
    async def supports_watch(self) -> bool:
        """Return True if native watching is supported."""
        pass
    
    @abstractmethod
    async def watch(self, path: str, callback: callable) -> WatchHandle:
        """
        Watch for changes (local only). 
        Cloud adapters should NOT implement this — use polling.
        """
        pass
```

### Implementations

| Adapter | Watching Strategy | Notes |
|---------|-------------------|-------|
| `LocalStorageAdapter` | watchdog (native inotify/FSEvents) | USB drives use same adapter, just different mount path |
| `USBStorageAdapter` | watchdog (local) | Extends Local, auto-detects mount points |
| `GoogleDriveAdapter` | API polling | `changes.list` endpoint with `startPageToken` |
| `OneDriveAdapter` | API polling | `delta` API for change tracking |
| `RcloneAdapter` | **Not recommended for watching** | Use for read/write only, not watching |

### Hybrid Watching Architecture

```
WatchManager
├── LocalWatchManager (watchdog-based)
│   └── Observer per watched path
│       └── EventHandler → triggers Ingestion Agent
└── CloudWatchManager (polling-based)
    └── Scheduler (asyncio)
        └── Periodic tasks per cloud watch
            └── list_changes(since) → triggers Ingestion Agent
```

**Configuration:**
```yaml
watches:
  - path: /home/user/documents
    backend: local
    recursive: true
    
  - path: gdrive://Research
    backend: gdrive
    poll_interval: 300  # seconds
    
  - path: onedrive://Work
    backend: onedrive
    poll_interval: 600
```

---

## 7. Vector Store Abstraction

### ABC Interface

```python
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any

class VectorStore(ABC):
    """Abstract vector database interface."""
    
    @abstractmethod
    async def initialize(self, collection_name: str, embedding_dim: int) -> None:
        """Initialize connection and collection."""
        pass
    
    @abstractmethod
    async def add_documents(
        self, 
        ids: List[str], 
        embeddings: List[List[float]], 
        metadatas: List[Dict],
        documents: List[str]
    ) -> None:
        """Add or update documents."""
        pass
    
    @abstractmethod
    async def search(
        self, 
        query_embedding: List[float], 
        filter_dict: Optional[Dict] = None,
        top_k: int = 10,
        include: List[str] = ["metadatas", "documents", "distances"]
    ) -> List[Dict[str, Any]]:
        """Vector similarity search with optional metadata filtering."""
        pass
    
    @abstractmethod
    async def delete(self, ids: List[str]) -> None:
        """Delete documents by ID."""
        pass
    
    @abstractmethod
    async def get(self, ids: List[str]) -> List[Dict[str, Any]]:
        """Retrieve documents by ID."""
        pass
    
    @abstractmethod
    async def count(self) -> int:
        """Get total document count."""
        pass
```

### Implementations

| Implementation | Backend | Tradeoff |
|----------------|---------|----------|
| `ChromaDBStore` | ChromaDB | Embedded, no ops, metadata filtering, millions of vectors |
| `QdrantStore` | Qdrant | 10x+ throughput, multi-vector support, one-stage HNSW filtering |

**Migration Path:** Change `vector_store.type` in config, rerun ingestion (or export/import vectors).

---

## 8. Enhanced Components

### 8.1 Reranker Module

```python
class Reranker(ABC):
    @abstractmethod
    async def rerank(self, query: str, documents: List[str], top_k: int = 5) -> List[int]:
        """Return indices of top-k most relevant documents."""
        pass

class CrossEncoderReranker(Reranker):
    """Uses sentence-transformers cross-encoder."""
    # Default: ms-marco-MiniLM-L-6-v2
    # Upgrade: bge-reranker-base, bge-reranker-large
```

**Usage in Query Pipeline:**
1. Vector search returns Top 50
2. Reranker scores all 50 against query
3. Top 5 passed to LLM context

### 8.2 Full-Text Search

**SQLite FTS5 (dev):**
- Native to SQLite, no additional dependencies
- Good for single-user deployments

**PostgreSQL FTS (prod):**
- `to_tsvector()` / `to_tsquery()`
- Supports weighted fields (title weighted higher than body)
- Can combine with pgvector for hybrid search

**Hybrid Search Interface:**
```python
async def hybrid_search(query: str, filters: dict, alpha: float = 0.7):
    """
    Alpha: blending weight (0.0 = pure FTS, 1.0 = pure vector)
    """
    vector_results = await vector_search(query, top_k=50)
    fts_results = await full_text_search(query, top_k=50)
    return merge_and_rerank(vector_results, fts_results, alpha)
```

### 8.3 Caching Layer

**Two-Tier Cache:**

1. **Embedding Cache:** Query string → embedding vector
   - Key: `SHA256(normalized_query)`
   - TTL: 7 days
   - Storage: DiskCache (SQLite-backed)

2. **Result Cache:** Query hash → search results
   - Key: `SHA256(query + filters + top_k + rerank_model)`
   - TTL: 24 hours
   - Storage: DiskCache or Redis

3. **Generation Cache:** Content request → generated content
   - Key: `SHA256(doc_ids + content_type + params)`
   - TTL: 30 days
   - Storage: Database (long-term)

**Cache Invalidation:**
- Manual: `grimoire cache clear`
- Automatic: When source documents are updated/reingested
- Scheduled: Background cleanup of expired entries

### 8.4 Conflict Resolution

**Conflict Detection:**
```python
if existing_doc.file_hash == new_file_hash:
    return "skip"  # No change
elif existing_doc.updated_at < new_file_mtime:
    return "update"  # File modified
else:
    return "version_conflict"  # Race condition?
```

**Resolution Strategies:**
- `auto` (default): Update if file is newer
- `skip`: Keep existing
- `duplicate`: Store both with versioning
- `manual`: Flag for review

**Versioning:**
- Soft delete old chunks (mark as `superseded_by`)
- New ingestion gets new chunk IDs
- `documents.version` field increments

### 8.5 Rate Limiting & Throttling

**Token Bucket per Backend:**
- Local: No limit
- Cloud APIs: Respect rate limits (e.g., GDRIVE_READ_OPS = 1000/hour)
- Ollama: Configurable concurrency (default: 4 concurrent generations)

**Implementation:**
```python
@rate_limit(backend="gdrive", requests=1000, window=3600)
async def list_files_gdrive(...):
    pass
```

### 8.6 Observability

**Tracing with OpenTelemetry or LangSmith:**

| Span | Attributes |
|------|------------|
| `ingestion` | doc_type, file_size, duration, chunk_count |
| `query` | query_length, retrieval_time, rerank_time, llm_time |
| `generation` | content_type, model, tokens_used, cached |

**Metrics (Prometheus/Grafana optional):**
- Documents indexed
- Queries per minute
- Cache hit rates
- Processing latency (p50, p95, p99)
- Cloud API quotas used

**Logging:**
- Structured JSON logs
- Processing audit trail in database
- Error context for debugging

---

## 9. Tagging / Categorization System

### Hierarchical Categories

- Self-referential parent-child relationships
- Unlimited depth (but UI may limit to 2-3)
- Color coding for visual distinction
- Slug for URL-safe identifiers

### Multi-Tag Assignment

| Source | Confidence | Behavior |
|--------|------------|----------|
| **LLM** | 0.0-1.0 | Suggests tags, user can confirm |
| **User** | 1.0 | Manual override, always authoritative |
| **Rule** | 1.0 | Pattern-based (e.g., `**/finance/**` → "Finance") |

### Auto-Tagging Logic

```python
async def auto_tag(document: Document, categories: List[Category]):
    # Get category descriptions
    category_context = format_categories(categories)
    
    # Get document sample (first 2000 chars of text)
    sample = document.chunks[:3]  # First few chunks
    
    # LLM prompt: "Categorize this document into one or more of the following categories..."
    suggested_tags = await llm_categorize(sample, category_context)
    
    # Store with confidence scores
    for tag, confidence in suggested_tags:
        if confidence > threshold:  # e.g., 0.7
            await add_tag(document, tag, confidence, tagged_by="llm")
```

### CLI Commands

```bash
grimoire category add "Research" --description "Research papers and notes"
grimoire category add "AI/ML" --parent "Research" --color "#3498db"
grimoire category list --tree
grimoire category delete "Obsolete" --reassign-to "Archive"

grimoire tag 42 "AI/ML" --confidence 0.95
grimoire tag 42 "Important" --manual
grimoire untag 42 "OldTag"

grimoire tags suggest 42  # Show LLM suggestions, don't apply
grimoire tags apply 42 --auto  # Apply high-confidence suggestions
```

---

## 10. Configuration (Pydantic Settings)

### Environment Variables / grimoire.yaml

```yaml
# LLM Configuration
grimoire:
  llm:
    model: "llama3"                    # Ollama model name
    url: "http://localhost:11434"      # Ollama base URL
    temperature: 0.7
    max_tokens: 4096
  
  # Embeddings - Configurable with sensible defaults
  embeddings:
    model: "sentence-transformers/all-mpnet-base-v2"  # Upgraded from all-MiniLM
    fallback_model: "sentence-transformers/all-MiniLM-L6-v2"
    device: "auto"                      # cuda, cpu, mps, or auto
    batch_size: 32
    
    # Per-index overrides supported
    indices:
      technical:
        model: "BAAI/bge-base-en-v1.5"  # Better for technical papers
      general:
        model: "sentence-transformers/all-mpnet-base-v2"
  
  # Database
  database:
    url: "postgresql://user:pass@localhost/grimoire"  # Production: PostgreSQL 16+
    dev_url: "sqlite:///grimoire.db"                   # Dev/testing only
    echo: false                                        # SQL logging
    pool_size: 10                                      # PostgreSQL only
    
  # Logging (loguru)
  logging:
    level: "INFO"                                      # DEBUG, INFO, WARNING, ERROR
    format: "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}"
    rotation: "1 week"                                 # Rotate logs weekly
    retention: "1 month"                               # Keep 1 month of logs
    log_dir: "./log"                                   # Dev: ./log, Prod: /var/log/grimoire
    
  # Celery (Distributed Task Queue)  
  celery:
    broker_url: "redis://localhost:6379/0"
    result_backend: "redis://localhost:6379/1"
    task_serializer: "json"
    accept_content: ["json"]
    result_serializer: "json"
    timezone: "UTC"
    enable_utc: true
    
  # Redis (Cache & Celery)
  redis:
    host: "localhost"
    port: 6379
    db_cache: 2                                        # DB for cache
  
  # Vector Store
  vector_store:
    type: "chromadb"                    # or "qdrant"
    
    # ChromaDB options
    chromadb:
      path: "./chroma_db"
      collection_name: "documents"
      
    # Qdrant options (for future migration)
    qdrant:
      url: "http://localhost:6333"
      api_key: null
      collection_name: "documents"
  
  # Chunking
  chunking:
    default_strategy: "semantic"        # semantic, markdown, recursive
    chunk_size: 1000                    # tokens (approximate)
    chunk_overlap: 200                  # tokens
    
    # Strategy-specific settings
    semantic:
      threshold: 0.5                    # Cosine similarity threshold for splits
      min_chunk_size: 100             # Characters
    
    markdown:
      headers_to_split_on: ["#", "##", "###"]
  
  # Processing
  processing:
    parse_pdf_ocr: true
    parse_images: true
    auto_tag_threshold: 0.7             # Min confidence for auto-tagging
    dedup_strategy: "hash"              # hash or content
    concurrency: 4                       # Parallel processing workers
    rate_limit_cloud: true
  
  # Query
  query:
    default_top_k: 10
    rerank_top_k: 50                   # Retrieve more, rerank down
    rerank_model: "cross-encoder/ms-marco-MiniLM-L-6-v2"
    hybrid_alpha: 0.7                  # Vector vs FTS weight
    enable_citations: true
  
  # Caching
  cache:
    embedding_ttl: 604800              # 7 days
    result_ttl: 86400                   # 1 day
    generation_ttl: 2592000           # 30 days
    storage: "disk"                    # disk or redis
    path: "./cache"
  
  # Cloud Storage
  cloud:
    google:
      credentials_path: "~/.config/gcloud/credentials.json"
      token_store: "~/.config/grimoire/gdrive_tokens.json"
    
    onedrive:
      client_id: "${ONEDRIVE_CLIENT_ID}"
      client_secret: "${ONEDRIVE_SECRET}"
      token_store: "~/.config/grimoire/onedrive_tokens.json"
  
  # Watch Configuration
  watch:
    default_poll_interval: 300          # 5 minutes for cloud
    max_local_watches: 100              # inotify limit consideration
    ignore_patterns:
      - "*.tmp"
      - ".git/"
      - "__pycache__/"
  
  # Observability
  observability:
    log_level: "INFO"
    structured_logs: true
    tracing: false                       # Enable for LangSmith/OpenTelemetry
    metrics: false                       # Prometheus export
```

---

## 11. Project Structure

```
grimoire/
├── cli/                           # Click CLI commands
│   ├── __init__.py
│   ├── main.py                    # CLI entry point
│   ├── ingest.py                  # scan, ingest commands
│   ├── watch.py                   # watch/unwatch commands
│   ├── query.py                   # ask, search commands
│   ├── generate.py                # summary, flashcards commands
│   ├── categories.py              # category/tag management
│   ├── config.py                  # configuration commands
│   └── status.py                  # stats commands
│
├── api/                           # FastAPI REST API
│   ├── __init__.py
│   ├── main.py
│   ├── dependencies.py            # FastAPI deps (db, config)
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── ingest.py
│   │   ├── query.py
│   │   ├── documents.py
│   │   ├── categories.py
│   │   └── watch.py
│   └── schemas.py                 # Pydantic models for API
│
├── agents/                        # LangChain Deep Agents
│   ├── __init__.py
│   ├── base.py                    # Shared agent utilities
│   ├── ingestion.py               # Ingestion agent
│   ├── watcher.py                 # Watcher agent (manages sub-watchers)
│   ├── query.py                   # Query/RAG agent
│   └── content_gen.py             # Content generation agent
│
├── core/                          # Core business logic
│   ├── __init__.py
│   ├── chunker.py                 # Chunking strategies
│   │   ├── base.py
│   │   ├── semantic.py
│   │   ├── markdown.py
│   │   └── recursive.py
│   ├── embedder.py                # Embedding generation
│   ├── parser.py                  # Docling wrapper
│   ├── tagger.py                  # LLM categorization
│   ├── reranker.py                # Cross-encoder reranking
│   ├── dedup.py                   # Deduplication logic
│   └── cache.py                   # Cache management
│
├── storage/                       # Storage adapters
│   ├── __init__.py
│   ├── base.py                    # StorageAdapter ABC
│   ├── local.py                   # Local filesystem + USB
│   ├── gdrive.py                  # Google Drive API
│   ├── onedrive.py                # OneDrive API
│   └── watch_manager.py           # Hybrid watch (local + cloud)
│
├── vectorstore/                   # Vector store abstraction
│   ├── __init__.py
│   ├── base.py                    # VectorStore ABC
│   ├── chromadb.py                # ChromaDB implementation
│   └── qdrant.py                  # Qdrant implementation (future)
│
├── search/                        # Search components
│   ├── __init__.py
│   ├── vector.py                  # Vector search wrapper
│   ├── fulltext.py                # FTS (SQLite/Postgres)
│   └── hybrid.py                  # Hybrid search orchestration
│
├── strategies/                    # Domain-specific chunking + retrieval
│   ├── __init__.py                # Public re-exports (BaseChunker, BaseRetriever, get_chunker_for)
│   ├── base.py                    # Abstract types: BaseRetriever ABC, get_chunker_for registry
│   └── security/                  # Security-domain pipeline (Phases 1–6 complete)
│       ├── __init__.py
│       ├── corpus.py              # SourceType enum + detect_source_type()
│       ├── metadata.py            # SecurityMetadata, Severity, TLPLevel
│       ├── extractor.py           # LLM metadata extractor for prose
│       ├── chunker.py             # SecurityChunker (Sigma/NVD/MITRE/prose dispatch)
│       └── parsers/
│           ├── __init__.py
│           ├── sigma.py           # parse_sigma()
│           ├── nvd.py              # parse_cve(), parse_nvd_json()
│           └── mitre.py           # parse_mitre()
│
├── db/                            # Database layer
│   ├── __init__.py
│   ├── models.py                  # SQLAlchemy ORM
│   ├── session.py                 # DB session management
│   ├── migrations/                # Alembic migrations
│   │   └── env.py
│   └── repositories/            # Repository pattern (optional)
│       ├── __init__.py
│       ├── documents.py
│       └── categories.py
│
├── config/                        # Configuration
│   ├── __init__.py
│   ├── settings.py                # Pydantic Settings
│   └── default_categories.yaml    # Seed taxonomy
│
├── utils/                         # Shared utilities
│   ├── __init__.py
│   ├── hash.py                    # SHA-256, caching keys
│   ├── path.py                    # Path normalization
│   ├── rate_limit.py              # Token bucket
│   └── observability.py           # Logging, tracing
│
├── tests/                         # Test suite
│   ├── conftest.py
│   ├── unit/
│   │   ├── test_chunker.py
│   │   ├── test_embedder.py
│   │   ├── test_storage.py
│   │   └── test_cache.py
│   ├── integration/
│   │   ├── test_pipeline.py
│   │   └── test_agents.py
│   └── fixtures/                  # Test data
│       └── sample_docs/
│
├── docs/
│   └── DESIGN.md                  # This document
│
├── alembic.ini                    # DB migration config
├── pyproject.toml                 # Project config & dependencies
├── .env.example                   # Environment template
├── .gitignore
└── README.md                      # Updated project README
```

### pyproject.toml (uv-based)

```toml
[project]
name = "grimoire"
version = "2.0.0"
description = "Agent-based knowledge management system"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "click>=8.1",
    "fastapi>=0.110",
    "uvicorn[standard]>=0.28",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "chromadb>=0.4",
    "langchain-community>=0.2",
    "langchain-ollama>=0.0.1",
    "docling>=2.0",
    "watchdog>=4.0",
    "sentence-transformers>=2.5",
    "loguru>=0.7",
    "redis>=5.0",
    "diskcache>=5.6",
    "celery>=5.3",
    "httpx>=0.27",
    "aiofiles>=23.2",
    "python-magic>=0.4",
    "aiohttp>=3.9",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=4.1",
    "mypy>=1.9",
    "ruff>=0.3",
    "black>=24.2",
    "pre-commit>=3.6",
]

[project.scripts]
grimoire = "grimoire.cli.main:main"

[tool.hatch.build.targets.wheel]
packages = ["grimoire"]

[tool.black]
line-length = 88
target-version = ['py312']
include = '\.pyi?$'

[tool.ruff]
line-length = 88
target-version = "py312"

[tool.ruff.lint]
select = [
    "E",  # pycodestyle errors
    "W",  # pycodestyle warnings
    "F",  # Pyflakes
    "I",  # isort
    "N",  # pep8-naming
    "W",  # pycodestyle
    "UP", # pyupgrade
    "B",  # flake8-bugbear
    "C4", # flake8-comprehensions
    "SIM", # flake8-simplify
]

[tool.mypy]
python_version = "3.12"
strict = true
warn_return_any = true
warn_unused_ignores = true
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

---

## 12. CLI Usage Examples

```bash
# Configuration
grimoire config init                    # Interactive setup
grimoire config show                    # Display current config
grimoire config edit                    # Open in $EDITOR

# Scan and ingest
grimoire ingest /path/to/documents --recursive --strategy semantic
grimoire ingest gdrive://Research --poll-interval 300
grimoire ingest onedrive://Work --auto-tag

# Watch directories
grimoire watch /home/user/docs          # Local with watchdog
grimoire watch gdrive://Research --poll-interval 300
grimoire watch onedrive://Presentations --poll-interval 600
grimoire watch list                     # Show active watches
grimoire watch unwatch <watch-id>

# Query the knowledge base
grimoire ask "What are the key findings about neural networks?"
grimoire ask "Summarize the quarterly reports" --tag "2024" --tag "finance"
grimoire search "machine learning" --tag "research" --format json
grimoire search "project alpha" --type pdf --hybrid-alpha 0.5

# Generate content on-demand
grimoire generate summary --doc-id 42 --style detailed
grimoire generate flashcards --tag "biology" --count 20
grimoire generate cliff-notes --query "Q3 financial performance"
grimoire generate outline --tag "thesis" --format markdown

# Manage categories
grimoire category add "Research" --description "General research"
grimoire category add "AI/ML" --parent "Research" --color "#3498db"
grimoire category add "Deep Learning" --parent "AI/ML"
grimoire category list --tree
grimoire category delete "Old" --reassign-to "Archive"

# Tagging
grimoire tag 42 "AI/ML" "Important" --confidence 0.95
grimoire tag suggest 42               # Preview auto-tags
grimoire tag auto 42 --threshold 0.8 # Apply high-confidence
grimoire untag 42 "OldTag"

# Status and maintenance
grimoire status                         # Show index stats
grimoire status --detailed              # Per-backend stats
grimoire docs list --tag "research" --type pdf
grimoire docs show 42                   # Full document details
grimoire cache clear                    # Clear query cache
grimoire cache stats                    # Show cache metrics
grimoire reindex 42                     # Reprocess document
grimoire migrate --to qdrant            # Future: vector store migration
```

---

## 13. Verification & Testing Plan

### Unit Tests
- `test_chunker.py` — Test all chunking strategies
- `test_embedder.py` — Test embedding model swapping
- `test_storage.py` — Test each storage adapter with mocks
- `test_cache.py` — TTL, invalidation, hit rates
- `test_reranker.py` — Ranking accuracy, performance

### Integration Tests
- `test_pipeline.py` — Full file → query pipeline
- `test_agents.py` — Agent tool calling, state management
- `test_hybrid_search.py` — Vector + FTS combination

### CLI Smoke Tests
```bash
# Run against temp directory with sample files
pytest tests/cli/ --sample-dir=tests/fixtures/sample_docs/
```

### Cross-Platform Testing Matrix
| Platform | Python | Test Focus |
|----------|--------|------------|
| Ubuntu 22.04 | 3.11 | Full suite |
| Windows 11 | 3.11 | Watchdog, path handling |
| macOS 14 | 3.11 | Basic functionality |

### Scale Tests
| Test | Documents | Success Criteria |
|------|-----------|------------------|
| Small | 100 | <5 min ingestion, <100ms query |
| Medium | 1,000 | <30 min ingestion, <200ms query |
| Large | 10,000 | <2 hour ingestion, <500ms query |
| Stress | 100,000 | 8 hour ingestion, <1s query |

### Cloud Sync Tests
- Google Drive: 100 files, verify polling catches changes
- OneDrive: 100 files, test delta sync
- Mixed local + cloud: Simultaneous ingestion

---

## 14. Migration Path from Current Grimoire

### Phase 1: Coexistence
- New schema alongside existing FAISS store
- `grimoire migrate` command exports existing data

### Phase 2: Data Migration
```bash
grimoire migrate --legacy-store ./faiss_index \
                 --target chromadb \
                 --re-chunk --strategy semantic
```

### Phase 3: API Compatibility
- Keep existing `app.py` endpoints as wrappers
- Deprecate gradually over 2 versions

---

## 15. Future Considerations

### Near Term (v2.x)
- [ ] Web UI (Streamlit or React + FastAPI)
- [ ] Mobile companion (read-only sync)
- [ ] Import/export (Obsidian, Notion, Roam)
- [ ] Plugin system for custom parsers

### Medium Term (v3.0)
- [ ] Distributed processing (Celery + Redis)
- [ ] Multi-user with RBAC
- [ ] Qdrant as default vector store
- [ ] Real-time collaboration (CRDTs)

### Research
- [ ] Knowledge graph visualization
- [ ] Automatic relationship discovery
- [ ] Federated search (multiple Grimoire instances)
- [ ] Local LLM fine-tuning on your corpus

---

## Appendix A: ChromaDB → Qdrant Migration Guide

### Detection Triggers
- Collection size exceeds 500K vectors
- Query latency p99 > 500ms
- Need multi-tenant collections
- Complex multi-field filtering requirements

### Migration Steps
1. Configure Qdrant connection
2. Run parallel indexing to Qdrant
3. Validation: compare sample queries
4. Switch `vector_store.type` in config
5. Monitor, fallback available

### Interface Compatibility
Both implement `VectorStore` ABC, so migration is:
1. Configure new backend
2. Reindex (or export/import vectors)
3. Update config
4. Restart

---

## Appendix B: Embedding Model Comparison

| Model | Size | Speed | Quality | Best For |
|-------|------|-------|---------|----------|
| all-MiniLM-L6-v2 | 80MB | Fast | Good | General use, speed critical |
| **all-mpnet-base-v2 (default)** | 440MB | Medium | **Better** | **Balanced quality/speed** |
| bge-base-en-v1.5 | 440MB | Medium | Excellent | Technical/retrieval tasks |
| nomic-embed-text-v2 | 550MB | Medium | Excellent | Instruction following |
| e5-large-v2 | 1.3GB | Slow | Best | Maximum quality |

**Recommendation:** Start with all-mpnet-base-v2, allow per-index customization for technical archives.

---

**End of Design Document**
