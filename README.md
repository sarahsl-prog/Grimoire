# Grimoire

**Agent-based Knowledge Management System**

Grimoire is a production-ready, modular knowledge management platform supporting 100K+ documents, hierarchical auto-tagging, multi-source cloud storage, and on-demand content generation. Built with Python 3.12+, FastAPI, LangChain Deep Agents, and modern ML tools.

## Features

- 🤖 **Agent-based architecture** - LangChain Deep Agents for document ingestion, watching, querying, and content generation
- 📄 **Multi-format support** - PDF, DOCX, PPTX, XLSX, HTML, images via Docling
- 🔍 **Hybrid search** - Vector similarity + PostgreSQL full-text search + reranking
- 🏷️ **Auto-tagging** - LLM-powered hierarchical categorization
- ☁️ **Cloud storage** - Local, USB, Google Drive, OneDrive with hybrid polling
- ⚡ **High performance** - Async I/O, Redis caching, configurable embedding models
- 🔒 **Privacy-first** - Local LLM via Ollama, offline capable
- 🧩 **MCP server** - Model Context Protocol (stdio + SSE) with tier-gated tool access for AI assistant integration

## Quick Start

### Prerequisites

- Python 3.12+
- Docker and Docker Compose
- uv package manager: `pip install uv`
- Ollama (for local LLM): [Installation Guide](https://ollama.com)

### Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/sarahsl-prog/Grimoire.git
   cd Grimoire
   ```

2. **Install dependencies**
   ```bash
   # Install all dependencies into a virtual environment
   uv sync
   ```

3. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your settings (see Configuration section below)
   ```

4. **Start infrastructure services**
   ```bash
   docker-compose up -d
   # Or bring up with optional tools: docker-compose --profile tools up -d
   ```

5. **Initialize the database**
   ```bash
   uv run alembic upgrade head
   ```

6. **Create categories** (optional, required for auto-tagging)

   Grimoire's LLM auto-tagger can only assign categories that already exist in the database. If you want documents to be automatically categorized during ingestion, create your categories first:
   ```bash
   grimoire category add "Research" --description "Research papers"
   grimoire category add "AI/ML" --parent research --color "#3498db"
   grimoire category add "Tutorials" --description "Guides and how-tos"
   ```

7. **Run the application**
   ```bash
   # CLI
   uv run grimoire --help

   # API server
   uv run uvicorn grimoire.api.main:app --reload --port 8001

   # MCP server (stdio mode for AI assistants like Claude Desktop)
   GRIMOIRE_API_KEY=your-key-here uv run grimoire mcp --stdio
   ```

### Development

```bash
# Run tests
uv run pytest

# Run linting
uv run ruff check .

# Format code
uv run black grimoire tests

# Type checking
uv run mypy --strict grimoire

# Run with coverage
uv run pytest --cov=grimoire --cov-report=html
```

## Configuration

Grimoire uses Pydantic Settings for configuration. Settings are loaded from:

1. Environment variables (highest priority)
2. `.env` file
3. `grimoire.yaml` configuration file (optional)
4. Default values (lowest priority)

See [.env.example](.env.example) for all available configuration options.

### Required Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `POSTGRES_USER` | PostgreSQL username | grimoire |
| `POSTGRES_PASSWORD` | PostgreSQL password | *required* |
| `POSTGRES_DB` | PostgreSQL database name | grimoire |
| `REDIS_HOST` | Redis server host | localhost |
| `REDIS_PORT` | Redis server port | 6379 |
| `OLLAMA_URL` | Ollama API endpoint | http://localhost:11434 |
| `EMBEDDING_MODEL` | Sentence-transformers model | sentence-transformers/all-mpnet-base-v2 |

## Usage

### CLI

```bash
# Ingest a single file
grimoire ingest /path/to/document.pdf --strategy semantic

# Ingest a directory recursively
grimoire ingest /path/to/documents --recursive --auto-tag

# Watch a directory for changes
grimoire watch start /home/user/docs --recursive

# Query the knowledge base (RAG pipeline)
grimoire ask "What are the key findings about neural networks?" --top-k 5

# Search without answer generation
grimoire search "neural networks" --top-k 10 --format json

# List ingested documents
grimoire docs list                                          # all docs
grimoire docs list --category "machine-learning"            # by category
grimoire docs list --search "quantization" --since 7d       # combined filters
grimoire docs list --format json                            # JSON output
grimoire docs list --format markdown                        # Markdown table

# Generate content (by doc ID or category)
grimoire generate summary -d doc-id --style detailed
grimoire generate flashcards -d doc-id --count 20
grimoire generate cliff-notes -d doc-id
grimoire generate outline -d doc-id
grimoire generate summary --category "machine-learning"
grimoire generate flashcards --category "cybersecurity"

# Manage categories
grimoire category add "Research" --description "Research papers"
grimoire category add "AI/ML" --parent research --color "#3498db"
grimoire category add "Deep Learning" --parent ai-ml
grimoire category list --tree
grimoire category remove "research"

# Compile wiki pages from ingested documents
grimoire wiki compile                                       # compile all pending
grimoire wiki compile -d <full-uuid>                        # compile one document
grimoire wiki compile --category "machine-learning"         # compile by category

# Browse and export wiki pages
grimoire wiki list                                          # list all pages (shows slugs)
grimoire wiki show <slug>                                   # display a page
grimoire wiki export                                        # export all pages to markdown
grimoire wiki export <slug>                                 # export a single page
grimoire wiki status                                        # show compile queue status
```

A **slug** is the URL-friendly identifier Grimoire generates from a page title (e.g. "Neural Networks" → `neural-networks`). Use `grimoire wiki list` to see the slug for each page.

Wiki pages are stored in the database and written to disk only when you run `grimoire wiki export`. Each export writes **all** compiled pages, overwriting any existing files — it is a full snapshot, not incremental. The export directory defaults to `wiki/` but can be pointed at an [Obsidian](https://obsidian.md) vault folder — cross-references are written as `[[Page Title]]` wiki-links, which Obsidian resolves natively:

```yaml
# grimoire.yaml
grimoire:
  wiki:
    wiki_pages_dir: "/path/to/your/obsidian/vault/Grimoire/"
```

```bash
# Tag/untag documents

grimoire tag doc-id biology science
grimoire untag doc-id science

# Configuration
grimoire config init          # Create default grimoire.yaml
grimoire config show          # Display current settings
grimoire config show llm      # Show specific section

# System status
grimoire status --detailed
grimoire cache stats
grimoire cache clear
```

### API

FastAPI automatically generates interactive API documentation:

- Swagger UI: http://localhost:8001/docs
- ReDoc: http://localhost:8001/redoc

```bash
# Health check
curl http://localhost:8001/health

# Ask a question (RAG pipeline)
curl -X POST http://localhost:8001/api/v1/query/ask \
  -H "Content-Type: application/json" \
  -d '{"query": "What is machine learning?", "top_k": 5}'

# Search without answer generation
curl -X POST http://localhost:8001/api/v1/query/search \
  -H "Content-Type: application/json" \
  -d '{"query": "neural networks", "top_k": 10}'

# Ingest a file
curl -X POST http://localhost:8001/api/v1/ingest/file \
  -H "Content-Type: application/json" \
  -d '{"file_path": "/path/to/document.pdf"}'

# Ingest a directory
curl -X POST http://localhost:8001/api/v1/ingest/directory \
  -H "Content-Type: application/json" \
  -d '{"directory": "/path/to/docs", "recursive": true}'

# List documents
curl http://localhost:8001/api/v1/documents

# Generate a summary
curl -X POST http://localhost:8001/api/v1/generate \
  -H "Content-Type: application/json" \
  -d '{"document_ids": ["doc-id"], "content_type": "summary"}'

# List categories
curl http://localhost:8001/api/v1/categories
```

### MCP (Model Context Protocol)

Grimoire exposes its full functionality as an MCP server, allowing AI assistants (Claude, Cursor, etc.) to query and manage your knowledge base natively.

**Available transports:**
- **stdio** – run `grimoire mcp --stdio` and point your AI client at it (requires `GRIMOIRE_API_KEY` env var)
- **SSE** – the API server mounts an MCP endpoint at `/mcp` (included automatically when you run `uvicorn grimoire.api.main:app`)

**Authentication:** All MCP requests require an `X-API-Key` header (SSE) or a valid `GRIMOIRE_API_KEY` env var (stdio). API keys have tier-based access control:

| Tier | Code: | Tools available |
|------|------|----------------|
| Read | `rdl` | search, ask, get_document, list_documents, list_categories, watch_status, pg_query, status |
| Dev  | `dvl` | Read + ingest_file, ingest_directory, generate, create_category, watch_start |
| Agent | `agt` | Dev + delete_document |

```bash
# List available tools via MCP
npx @anthropic-ai/mcp-inspector node build/index.js --method tools/list

# Example: search via MCP (SSE transport)
curl -N http://localhost:8001/mcp/sse \
  -H "X-API-Key: grim_agt_yourkey" \
  -H "Accept: text/event-stream"
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│              CLI / API / MCP Layer                 │
├─────────────────────────────────────────────────┤
│                  Agent Layer                     │
│    (Ingestion | Watcher | Query | Content Gen)  │
├──────────┬──────────┬───────────┬───────────────┤
│ Storage  │ Document │ Chunker   │ Vector Store │
│ Adapters │ Parser   │ Embedder  │ Reranker     │
├──────────┴──────────┴───────────┴───────────────┤
│              Storage / Persistence               │
│   PostgreSQL (metadata) │ ChromaDB (vectors)     │
└─────────────────────────────────────────────────┘
```

## Security mode

Grimoire ships a security-domain pipeline (Sigma / NVD CVE / MITRE ATT&CK chunkers, `SecurityRetriever` intent-aware re-rank, security filter surface) gated behind a single setting: `GRIMOIRE_SECURITY__DOMAIN=security`. The repo includes a ready-to-run Hetzner overlay — copy `.env.security.example`, run `scripts/security/seed_corpus.sh` to pull Sigma + MITRE + a year of NVD CVE, then `docker compose -f docker-compose.yml -f docker-compose.security.yml up -d`. See [docs/deploy/hetzner_security.md](docs/deploy/hetzner_security.md) for the full guide and [docs/strategies/](docs/strategies/) for the configuration / usage reference.

## Documentation

- [Design Document](docs/DESIGN.md) - Complete system architecture and design decisions
- [Implementation Plan](docs/IMPLEMENTATION.md) - Phased development roadmap
- [Security strategy](docs/strategies/README.md) — security-domain ingestion, retrieval, and filter surface
- [Hetzner security deploy](docs/deploy/hetzner_security.md) — one-shot homelab setup for the security pipeline
- [Coding Conventions](Claude.md) - Development guidelines and best practices

## Services (Docker Compose)

| Service | Port | Description |
|---------|------|-------------|
| PostgreSQL | 5432 | Primary metadata database |
| Redis | 6379 | Cache and message broker |
| ChromaDB | 8000 | Vector database |
| PGAdmin | 5050 | PostgreSQL management UI (optional) |
| Redis Commander | 8081 | Redis management UI (optional) |

## Testing

```bash
# Run all tests
uv run pytest

# Run with coverage report
uv run pytest --cov=grimoire --cov-report=html

# Run specific test categories
uv run pytest -m unit
uv run pytest -m integration
```

## Project Structure

```
grimoire/
├── cli/              # Click CLI commands
├── api/              # FastAPI REST API
├── agents/           # LangChain Deep Agents
├── core/             # Core business logic
├── storage/          # Storage adapters (local, cloud)
├── vectorstore/      # Vector store abstraction
├── search/           # Full-text and hybrid search
├── strategies/       # Domain-specific chunking + retrieval strategies (general / security)
├── db/               # Database models and migrations
├── config/           # Configuration management
└── utils/            # Shared utilities

tests/                # Test suite
├── unit/             # Unit tests
├── integration/      # Integration tests
└── fixtures/         # Test fixtures
```

## License

MIT License - See [LICENSE](LICENSE) for details

## Contributing

1. Create a feature branch: `git checkout -b feature/my-feature`
2. Make changes following our coding conventions
3. Run tests: `uv run pytest`
4. Run linting: `uv run ruff check . && uv run black grimoire tests`
5. Commit changes: `git commit -am "feat: add new feature"`
6. Push and create a Pull Request

## Support

- Issues: [GitHub Issues](https://github.com/sarahsl-prog/Grimoire/issues)
- Discussions: [GitHub Discussions](https://github.com/sarahsl-prog/Grimoire/discussions)

---

Built with ❤️ by Sarah (slgryph) and Grf (technogryphon)
