Context
The Grimoire repository currently has a minimal RAG system (2 Python files: app.py + rag_pipeline.py) with Streamlit UI, FAISS vector store, Ollama LLM, and PDF/TXT support. The user wants a complete redesign into a modular, agent-based knowledge management system that can:

Scan directories, detect file types, auto-tag content, and process for LLM consumption
Watch directories for new files and auto-process them
Support hierarchical multi-tag categorization
Let an LLM query, link, and generate content (summaries, flash cards, etc.) on-demand
Access local drives, OneDrive, Google Drive, USB — on Linux and Windows
Scale to 100K+ documents

User's Design Decisions

UI: CLI + API first (web UI later)
Scale: Large (100K+ documents) — needs Qdrant/FAISS + PostgreSQL-level metadata
Cloud Storage: Pluggable (both rclone mount and native APIs)
LLM Content Generation: On-demand (not auto-generated at ingestion)


Deliverable
Create /home/user/Grimoire/docs/DESIGN.md — a comprehensive architecture and design document covering all aspects of the redesigned system.

Steps
Step 1: Create the design document
Create docs/DESIGN.md with the full system design (content outlined below).
Step 2: Commit and push
Commit the design document to branch claude/redesign-rag-agent-cOVMJ and push.

Design Document Content Outline
1. System Architecture
Four-layer architecture:
┌─────────────────────────────────────────────────┐
│                   CLI / API Layer                │
│         (Click CLI + FastAPI REST API)           │
├─────────────────────────────────────────────────┤
│                  Agent Layer                     │
│   (LangGraph orchestration, tool-using agents)   │
├──────────┬──────────┬───────────┬───────────────┤
│ Ingestion│ Watcher  │ Query     │ Content Gen   │
│ Agent    │ Agent    │ Agent     │ Agent         │
├──────────┴──────────┴───────────┴───────────────┤
│                 Core Services                    │
│  Storage Adapters │ Document Parser │ Tagger     │
│  Chunker │ Embedder │ Metadata DB │ Vector Store │
├─────────────────────────────────────────────────┤
│              Storage / Persistence               │
│   PostgreSQL (metadata) │ ChromaDB (vectors)     │
│   Local FS │ rclone │ Native cloud APIs          │
└─────────────────────────────────────────────────┘
2. Technology Stack
LayerTechnologyRationaleCLIClickStandard Python CLI framework, composable commandsAPIFastAPIAsync, high-performance, auto-docs, background tasksAgent OrchestrationLangGraphState machines for agent workflows, cyclic graphs, self-correctionLLMOllama (any model)Local-first, model-agnosticEmbeddingssentence-transformers/all-MiniLM-L6-v2 (upgradeable)Keep current model, make swappableVector StoreChromaDBNative metadata filtering, embedded mode, scales to millions, no separate server neededMetadata DBSQLite (dev) / PostgreSQL (prod)SQLAlchemy ORM abstracts both; SQLite for single-user, Postgres for scaleDocument ParsingDoclingMIT license, best accuracy (97.9% complex tables), supports PDF/DOCX/PPTX/XLSX/images/HTML/audio, local execution, GPU accelerationFile WatchingwatchdogCross-platform (Linux/Windows/macOS), mature, event-drivenCloud StoragePluggable: rclone + native APIsStorageAdapter interface with multiple backendsTask Queueasyncio + background threads (MVP) / Celery (scale)Start simple, upgrade path clear
Key change from current stack: FAISS → ChromaDB. Rationale: FAISS has no metadata filtering (critical for tags/categories at 100K+ scale). ChromaDB provides native metadata filtering, embedded mode (no server), and handles millions of vectors on a single machine. This is the minimum viable change — Qdrant is the upgrade path if multi-tenant or complex filtering is needed later.
3. Data Model
PostgreSQL/SQLite Tables (via SQLAlchemy):

documents — id, source_path, storage_backend, file_type, file_hash, title, size_bytes, created_at, updated_at, processed_at, processing_status (pending/processing/completed/failed/stale), error_message
chunks — id, document_id (FK), chunk_index, content, token_count, vector_id (ChromaDB reference), created_at
categories — id, name, slug, parent_id (FK self-referential for hierarchy), description, created_at
document_tags — document_id (FK), category_id (FK), confidence (float), tagged_by (enum: llm/user/rule), created_at
generated_content — id, document_id (FK), content_type (enum: summary/flash_card/cliff_notes/image/outline), content, model_used, created_at
relationships — id, source_document_id (FK), target_document_id (FK), relationship_type (enum: related/references/summarizes/derived_from), confidence, created_at
watch_paths — id, path, storage_backend, recursive (bool), active (bool), last_scanned_at, created_at
processing_log — id, document_id (FK), action, status, details (JSON), created_at

4. Processing Pipeline
File Discovery (scan/watch)
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
Chunking (RecursiveCharacterTextSplitter, configurable)
    │
    ▼
Embedding (HuggingFace sentence-transformers)
    │
    ▼
Vector Storage (ChromaDB with metadata)
    │
    ▼
Auto-Tagging (LLM categorizes against existing taxonomy)
    │
    ▼
Metadata Storage (SQLAlchemy → SQLite/PostgreSQL)
    │
    ▼
Relationship Discovery (optional, on-demand)
5. Agent Design (LangGraph)
Four specialized agents, each a LangGraph StateGraph:

Ingestion Agent — Scans directories, processes files through the pipeline. Tools: list_files, detect_type, extract_content, chunk, embed, store, tag
Watcher Agent — Long-running daemon using watchdog. On file events, triggers Ingestion Agent for new/modified files. Tools: watch_directory, unwatch_directory, get_watch_status
Query Agent — Handles user questions with agentic RAG (retrieve → grade → rewrite → retrieve loop). Tools: vector_search, metadata_search, filter_by_tags, get_related_documents, get_document_details
Content Generation Agent — Creates derived content on-demand. Tools: generate_summary, generate_flash_cards, generate_cliff_notes, generate_outline, store_generated_content

Coordinator: A top-level LangGraph graph that routes user intent to the appropriate agent.
6. Storage Adapter Interface
pythonclass StorageAdapter(ABC):
    async def list_files(self, path, recursive) -> List[FileInfo]
    async def read_file(self, path) -> bytes
    async def write_file(self, path, data) -> None
    async def exists(self, path) -> bool
    async def get_metadata(self, path) -> FileMetadata
    async def watch(self, path, callback) -> WatchHandle
```

**Implementations:**
- `LocalStorageAdapter` — direct filesystem (covers USB drives too)
- `RcloneStorageAdapter` — uses rclone mount points or rclone commands
- `GoogleDriveAdapter` — native Google Drive API via google-api-python-client
- `OneDriveAdapter` — native Microsoft Graph API via msgraph-sdk

The system auto-detects which adapter to use based on path prefix or configuration.

### 7. Tagging / Categorization System

- Hierarchical: categories can have subcategories (self-referential FK)
- Multi-tag: documents can have multiple tags via junction table
- Three tagging sources: LLM auto-tag, user manual tag, rule-based tag
- Confidence scores on LLM-assigned tags
- Default seed categories configurable via YAML
- CLI commands: `grimoire category add`, `grimoire category list`, `grimoire tag <doc> <category>`

### 8. Project Structure
```
grimoire/
├── cli/                    # Click CLI commands
│   ├── __init__.py
│   ├── main.py             # CLI entry point
│   ├── ingest.py           # scan, ingest commands
│   ├── watch.py            # watch/unwatch commands
│   ├── query.py            # ask, search commands
│   ├── generate.py         # summary, flashcards commands
│   └── categories.py       # category/tag management
├── api/                    # FastAPI REST API
│   ├── __init__.py
│   ├── app.py
│   ├── routes/
│   │   ├── ingest.py
│   │   ├── query.py
│   │   ├── documents.py
│   │   └── categories.py
│   └── schemas.py          # Pydantic models
├── agents/                 # LangGraph agents
│   ├── __init__.py
│   ├── coordinator.py      # Top-level router
│   ├── ingestion.py        # Ingestion agent
│   ├── watcher.py          # File watcher agent
│   ├── query.py            # Query/RAG agent
│   └── content_gen.py      # Content generation agent
├── core/                   # Core business logic
│   ├── __init__.py
│   ├── parser.py           # Docling-based document parsing
│   ├── chunker.py          # Text splitting strategies
│   ├── embedder.py         # Embedding generation
│   ├── tagger.py           # LLM-based auto-tagging
│   └── dedup.py            # Deduplication via hashing
├── storage/                # Storage adapters
│   ├── __init__.py
│   ├── base.py             # Abstract StorageAdapter
│   ├── local.py            # Local filesystem
│   ├── rclone.py           # rclone-based cloud access
│   ├── gdrive.py           # Google Drive native API
│   └── onedrive.py         # OneDrive native API
├── db/                     # Database layer
│   ├── __init__.py
│   ├── models.py           # SQLAlchemy ORM models
│   ├── session.py          # DB session management
│   ├── migrations/         # Alembic migrations
│   └── vector.py           # ChromaDB wrapper
├── config/                 # Configuration
│   ├── __init__.py
│   ├── settings.py         # Pydantic Settings (env/yaml)
│   └── default_categories.yaml
├── tests/                  # Test suite
│   ├── test_parser.py
│   ├── test_agents.py
│   ├── test_storage.py
│   └── test_pipeline.py
├── docs/
│   └── DESIGN.md           # This design document
├── pyproject.toml          # Project config & dependencies
├── alembic.ini             # DB migration config
├── .env.example            # Environment template
└── README.md               # Updated project README
9. Configuration (Pydantic Settings)
Loaded from .env file, environment variables, or grimoire.yaml:

GRIMOIRE_LLM_MODEL — Ollama model name (default: llama3)
GRIMOIRE_EMBEDDING_MODEL — HuggingFace model (default: all-MiniLM-L6-v2)
GRIMOIRE_DB_URL — SQLAlchemy DB URL (default: sqlite:///grimoire.db)
GRIMOIRE_CHROMA_PATH — ChromaDB persistence directory
GRIMOIRE_OLLAMA_URL — Ollama base URL (default: http://localhost:11434)
GRIMOIRE_CHUNK_SIZE / GRIMOIRE_CHUNK_OVERLAP
GRIMOIRE_WATCH_PATHS — comma-separated paths to auto-watch
GRIMOIRE_RCLONE_REMOTES — configured rclone remote names

10. CLI Usage Examples
bash# Scan and ingest a directory
grimoire ingest /path/to/documents --recursive

# Watch a directory for new files
grimoire watch /path/to/documents
grimoire watch onedrive://Documents/Research

# Query the knowledge base
grimoire ask "What are the key findings about X?"
grimoire search --tag "research" --tag "2024" "machine learning"

# Generate content on-demand
grimoire generate summary --doc-id 42
grimoire generate flashcards --tag "biology"

# Manage categories
grimoire category add "Research" --parent "Academic"
grimoire category list --tree
grimoire tag 42 "Research" "Machine Learning"

# Status
grimoire status              # show index stats
grimoire docs list --tag "research" --type pdf
11. Verification Plan

Unit tests: Test each core module (parser, chunker, embedder, tagger, storage adapters)
Integration tests: Test full pipeline from file → vector store → query → response
CLI smoke tests: Run each CLI command against a test directory with sample files
Cross-platform: Test on both Linux and Windows (watchdog, path handling)
Scale test: Ingest 1K documents, verify query performance and tag filtering