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

6. **Run the application**
   ```bash
   # CLI
   uv run grimoire --help

   # API server
   uv run uvicorn grimoire.api.main:app --reload --port 8001
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
# Scan and ingest documents
grimoire ingest /path/to/documents --recursive --strategy semantic

# Watch a directory for changes
grimoire watch /home/user/docs --recursive

# Query the knowledge base
grimoire ask "What are the key findings about neural networks?"

# Generate content
grimoire generate summary --doc-id 42 --style detailed
grimoire generate flashcards --tag "biology" --count 20

# Manage categories
grimoire category add "Research" --description "Research papers"
grimoire category list --tree

# Get status
grimoire status
```

### API

FastAPI automatically generates interactive API documentation:

- Swagger UI: http://localhost:8001/docs
- ReDoc: http://localhost:8001/redoc

```bash
# Query via curl
curl -X POST http://localhost:8001/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is machine learning?", "top_k": 10}'
```

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   CLI / API Layer                │
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

## Documentation

- [Design Document](docs/DESIGN.md) - Complete system architecture and design decisions
- [Implementation Plan](docs/IMPLEMENTATION.md) - Phased development roadmap
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
