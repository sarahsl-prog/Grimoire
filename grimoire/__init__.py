"""Grimoire - Agent-based Knowledge Management System.

A production-ready, modular knowledge management platform supporting 100K+
documents, hierarchical auto-tagging, multi-source cloud storage, and
on-demand content generation.

Built with:
- Python 3.12+
- FastAPI for REST API
- LangChain Deep Agents for agentic workflows
- PostgreSQL for metadata
- ChromaDB/Qdrant for vector storage
- Ollama for local LLM inference
"""

__version__ = "2.0.0"
__author__ = "Sarah (slgryph)"
__license__ = "MIT"

from pathlib import Path

# Package root directory
PACKAGE_ROOT = Path(__file__).parent.absolute()
PROJECT_ROOT = PACKAGE_ROOT.parent.absolute()

# Default paths within the project
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "cache"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_CHROMADB_PATH = PROJECT_ROOT / "chroma_db"

__all__ = [
    "__version__",
    "__author__",
    "__license__",
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
    "DEFAULT_LOG_DIR",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_DATA_DIR",
    "DEFAULT_CHROMADB_PATH",
]
