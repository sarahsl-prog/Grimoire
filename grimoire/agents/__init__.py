"""LangChain Deep Agents for Grimoire.

This module provides four specialized agents:
- IngestionAgent: Document processing pipeline orchestration
- WatcherAgent: Directory monitoring and ingestion triggering
- QueryAgent: Agentic RAG question answering with hybrid search
- ContentGenerationAgent: On-demand derived content generation
"""

from grimoire.agents.content_gen import (
    ContentGenerationAgent,
    GenerationRequest,
    GenerationResult,
)
from grimoire.agents.ingestion import (
    BatchIngestionResult,
    IngestionAgent,
    IngestionResult,
)
from grimoire.agents.query import (
    Citation,
    QueryAgent,
    QueryResult,
    SearchOnlyResult,
)
from grimoire.agents.watcher import (
    WatcherAgent,
    WatcherStats,
    WatchStatus,
)

__all__ = [
    # Ingestion Agent
    "IngestionAgent",
    "IngestionResult",
    "BatchIngestionResult",
    # Watcher Agent
    "WatcherAgent",
    "WatcherStats",
    "WatchStatus",
    # Query Agent
    "QueryAgent",
    "QueryResult",
    "SearchOnlyResult",
    "Citation",
    # Content Generation Agent
    "ContentGenerationAgent",
    "GenerationRequest",
    "GenerationResult",
]
