# Grimoire Strategies

`grimoire/strategies/` is the home for domain-specific chunking and retrieval strategies. Grimoire ships a "general" pipeline (prose, mixed documents, the existing chunker stack) and is gaining a "security" pipeline focused on structured threat-intel sources (Sigma rules, NVD CVEs, MITRE ATT&CK, IOC lists). The strategies package provides the small set of abstract types — chunker base, retriever base, and a chunker registry helper — that both pipelines satisfy so the rest of Grimoire can stay agnostic of which domain is in use.

## Current status

**Phases 0–8 of the [security strategy plan](../plans/security_strategy_plan.md) have landed.** All scaffolding, source-type detection, security-metadata schema, Sigma/NVD/MITRE parsers + chunkers, the LLM metadata extractor, the `SecurityRetriever` post-fusion re-rank wrapper, and the strategy loader (`settings.security.domain` switch) are merged.

Domain selection is **not yet wired** into ingestion or query; that arrives in Phase 8.

Follow-on phases (one-line each — see the plan for full detail):

- **Phase 0 — done** — Strategy scaffolding (`grimoire/strategies/` package, `BaseRetriever` ABC, `Chunk` extended with `chunk_type` / `source_type`).
- **Phase 1 — done** — Source-type detection (`security/corpus.py`). See [`source_types.md`](source_types.md).
- **Phase 2 — done** — Metadata schema additions (`security/metadata.py`, `documents` columns + JSONB blob, ChromaDB metadata merge). See [`metadata.md`](metadata.md).
- **Phase 3 — done** — Sigma rule chunker + parser (`security/chunker.py`, `security/parsers/sigma.py`). See [`chunking.md`](chunking.md).
- **Phase 4 — done** — NVD CVE chunker + parser (`security/parsers/nvd.py`). See [`chunking.md`](chunking.md).
- **Phase 5 — done** — MITRE ATT&CK chunker + parser (`security/parsers/mitre.py`). See [`chunking.md`](chunking.md).
- **Phase 6 — done** — Prose fallback + LLM metadata extractor (`security/extractor.py`). See [`extractor.md`](extractor.md).
- **Phase 7 — done** — `SecurityRetriever` post-fusion re-rank wrapper (`security/retriever.py`). See [`retriever.md`](retriever.md).
- **Phase 8 — done** — Strategy loader + ingestion/query wiring (`strategies/loader.py`). See [`configuration.md`](configuration.md).
- **Phase 9** — Query agent + filter documentation.
- **Phase 10** — Hetzner deploy: compose tweaks, `.env.security.example`, deploy doc.

## What lives here

| Path | Purpose |
| --- | --- |
| `grimoire/strategies/__init__.py` | Public re-exports (`BaseChunker`, `BaseRetriever`, `get_chunker_for`). |
| `grimoire/strategies/base.py` | Abstract types: `BaseChunker` alias, `BaseRetriever` ABC, `get_chunker_for` registry stub. |
| `grimoire/strategies/security/` | Security-domain implementations (placeholder in Phase 0; Phases 1-7 fill it in). |

## Key design decisions

- **No parallel chunker hierarchy.** `BaseChunker` is an alias for the existing `grimoire.core.chunker.base.Chunker` ABC. Domain chunkers subclass the same base every other chunker uses.
- **Retrievers compose, not replace.** `BaseRetriever` implementations are expected to wrap `grimoire.search.hybrid.HybridSearch` rather than reimplement merging and reranking.
- **`Chunk` is extended, not forked.** Two optional fields — `chunk_type` and `source_type` — were added to the existing Pydantic `Chunk` model in Phase 0. Existing chunkers keep working with their default `None` values.
