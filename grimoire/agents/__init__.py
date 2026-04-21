"""Grimoire agents package.

Provides six specialised agents:
- IngestionAgent: Document processing pipeline orchestration
- WatcherAgent: Directory monitoring and ingestion triggering
- QueryAgent: Agentic RAG question answering with hybrid search
- ContentGenerationAgent: On-demand derived content generation
- WikiAgent: Wiki compilation from ingested documents
- CoordinatorAgent: Top-level router that dispatches to the above agents
"""

from grimoire.agents.content_gen import (
    ContentGenerationAgent,
    GenerationRequest,
    GenerationResult,
)
from grimoire.agents.coordinator import (
    CoordinatorAgent,
    CoordinatorContext,
    CoordinatorResult,
    IntentType,
    classify_intent,
    extract_content_type,
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
from grimoire.agents.wiki import (
    CompileResult,
    ContradictionAction,
    ContradictionResult,
    EntityExtraction,
    WikiAgent,
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
    # Wiki Agent
    "WikiAgent",
    "CompileResult",
    "EntityExtraction",
    "ContradictionResult",
    "ContradictionAction",
    # Coordinator Agent
    "CoordinatorAgent",
    "CoordinatorContext",
    "CoordinatorResult",
    "IntentType",
    "classify_intent",
    "extract_content_type",
]
