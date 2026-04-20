# Wiki Tool Design Spec

**Date:** 2026-04-20
**Status:** Approved

---

## Overview

The wiki tool compiles raw source documents into a structured, cross-referenced wiki. When a document is ingested, the LLM reads it, extracts entities and concepts, creates or updates wiki pages, flags contradictions with previous sources, and maintains cross-references. Knowledge gets compiled once, then stays compiled.

### Three Layers

1. **Raw sources** — immutable documents. The LLM reads from these but never modifies them. Source of truth.
2. **The wiki** — a directory of LLM-generated markdown files. Summaries, entity pages, concept pages. The LLM owns this layer entirely. You read it; the LLM writes and maintains it.
3. **The schema** — configuration that tells the LLM how the wiki is structured and what workflows to follow. Makes the LLM a disciplined wiki maintainer instead of a generic chatbot.

### Architecture Decisions

- **Page granularity:** One page per entity/concept
- **Storage:** Dedicated `WikiPage` DB model (not reusing `GeneratedContent`)
- **Update strategy:** Section-level updates with diff notes on how the new source differs from the previous source. All sections reference the original document.
- **Contradiction policy:** Hybrid — newer-wins for factual conflicts and temporal drift; source-priority for scope mismatches and terminology shifts
- **Trigger:** Hybrid — ingestion flags documents as wiki-pending; explicit `grimoire wiki compile` command compiles them

---

## Data Model

### WikiPage

The core entity. One page per entity/concept discovered by the LLM.

| Field | Type | Notes |
|---|---|---|
| id | UUID | Primary key |
| title | str | Unique. e.g. "Authentication Pipeline" |
| slug | str | Unique. e.g. "authentication-pipeline" |
| content | str | Assembled markdown (rendered from WikiPageSection rows on save) |
| version | int | Incremented on each update |
| status | WikiPageStatus | DRAFT, COMPILED, FLAGGED |
| entity_type | str, nullable | "concept", "component", "process", "entity" |
| created_at | datetime | Timezone-aware |
| updated_at | datetime | Timezone-aware |

### WikiPageSection

Sections within a page for granular updates and diff tracking.

| Field | Type | Notes |
|---|---|---|
| id | UUID | Primary key |
| wiki_page_id | FK | References WikiPage |
| heading | str | e.g. "Overview", "Configuration" |
| content | str | Section body (markdown) |
| section_index | int | Ordering within page |
| source_document_id | FK | Which source produced this section |
| source_priority | int | Priority of that source at compile time |
| contradiction_flag | str, nullable | Description of conflict, or None |
| superseded_by_section_id | FK (self), nullable | If newer-wins replaced this |
| created_at | datetime | Timezone-aware |
| updated_at | datetime | Timezone-aware |

### WikiCrossReference

Links between wiki pages.

| Field | Type | Notes |
|---|---|---|
| id | UUID | Primary key |
| source_page_id | FK | References WikiPage |
| target_page_id | FK | References WikiPage |
| ref_type | WikiRefType | REFERENCES, DEPENDS_ON, RELATED_TO, CONTRADICTS |
| context | str, nullable | Why the link exists |
| created_at | datetime | Timezone-aware |

Unique constraint on (source_page_id, target_page_id, ref_type).

### WikiCompileJob

Tracks which documents need compilation and which have been processed.

| Field | Type | Notes |
|---|---|---|
| id | UUID | Primary key |
| document_id | FK | References Document |
| status | CompileStatus | PENDING, COMPILING, COMPLETED, FAILED |
| compiled_at | datetime, nullable | When compilation finished |
| error_message | str, nullable | Failure reason if FAILED |

### New Enums

- `WikiPageStatus`: DRAFT, COMPILED, FLAGGED
- `WikiRefType`: REFERENCES, DEPENDS_ON, RELATED_TO, CONTRADICTS
- `CompileStatus`: PENDING, COMPILING, COMPLETED, FAILED

### Design Rationale

- `WikiPageSection.source_document_id` ties every section back to its raw source
- `WikiPageSection.contradiction_flag` stores the conflict description when detected
- `WikiPageSection.superseded_by_section_id` enables newer-wins replacement with a trail
- `WikiCrossReference` is separate from `Relationship` because wiki refs are between compiled pages, not raw documents
- Circular cross-references are allowed (wiki pages can mutually reference each other)

---

## WikiAgent — The Compilation Engine

The LLM-driven component that reads sources, decides what wiki pages to create/update, handles contradictions, and maintains cross-references.

### Key Methods

```python
WikiAgent
  compile_document(document_id) -> WikiCompileResult
  compile_pending() -> list[WikiCompileResult]
  _identify_entities(chunks) -> list[EntityExtraction]
  _match_existing_page(entity) -> WikiPage | None
  _generate_page(entity, source_doc, existing_page | None) -> WikiPage
  _update_sections(existing_page, new_content, source_doc) -> WikiPage
  _detect_contradictions(existing_sections, new_section) -> ContradictionResult
  _apply_contradiction_policy(contradiction) -> ContradictionAction
  _discover_cross_references(page) -> list[WikiCrossReference]
```

### Compilation Flow (single document)

1. Fetch document + chunks from DB
2. LLM call: identify entities/concepts in the source, returns list of `{name, type, summary}` tuples
3. For each entity:
   a. Find existing WikiPage by slug match
   b. If no page: LLM generates new page with sections
   c. If page exists:
      - LLM reads existing sections + new source content
      - For each section the new source touches:
        - Detect contradictions
        - Apply policy (newer-wins or source-priority)
        - If newer-wins: mark old section as superseded, create new section
        - If source-priority: higher priority wins, flag if lower-priority source conflicts
      - Add/update sections with source_document_id reference
   d. Discover cross-references to other wiki pages
4. Write all changes to DB (single transaction per document)
5. Mark WikiCompileJob as COMPLETED

### EntityExtraction

What the LLM returns from step 2:

| Field | Type | Notes |
|---|---|---|
| name | str | "Authentication Pipeline" |
| entity_type | str | "process", "concept", "component", "entity" |
| summary | str | 1-2 sentence description |
| confidence | float | How central this entity is to the source |

### Contradiction Detection

The LLM compares each new section against existing ones:

| Field | Type | Notes |
|---|---|---|
| conflict_type | str | "factual", "temporal", "scope", "terminology" |
| description | str | What the conflict is |
| existing_claim | str | What the wiki currently says |
| new_claim | str | What the new source says |
| severity | str | "high", "medium", "low" |

### Contradiction Policy Routing

- `factual` or `temporal`: newer-wins — supersede old section, keep in history
- `scope` or `terminology`: source-priority — compare source_priority values, higher wins, loser gets flagged

---

## Config, CLI, and Coordinator Integration

### WikiConfig

New config section in `GrimoireSettings`:

| Field | Type | Default | Notes |
|---|---|---|---|
| enabled | bool | True | Master switch |
| compile_on_ingest | bool | False | Auto-flag docs as wiki-pending |
| source_priorities | dict[str, int] | {} | e.g. {"architecture-dec": 10, "meeting-notes": 3} |
| default_entity_types | list[str] | ["concept", "component", "process", "entity"] | What entity types the LLM extracts |
| max_sections_per_page | int | 10 | Cap on sections per page |
| max_compile_batch_size | int | 20 | Docs per compile_pending() run |
| compile_model | str, nullable | None | Override LLM model for wiki (else uses llm.model) |
| wiki_pages_dir | str | "wiki/" | Where to export markdown files |

Source priorities can be set in `grimoire.yaml` or via env vars (`GRIMOIRE_WIKI__SOURCE_PRIORITIES__ARCHITECTURE_DEC=10`).

### CLI Commands

```
grimoire wiki compile                    — compile all wiki-pending documents
grimoire wiki compile --doc-id <id>       — compile a specific document
grimoire wiki compile --category <slug>   — compile docs in a category
grimoire wiki list                        — list all wiki pages
grimoire wiki show <slug>                — display a wiki page
grimoire wiki export                     — export all pages to markdown files
grimoire wiki export <slug>              — export single page
grimoire wiki status                     — show compile queue counts
```

### Coordinator Integration

- Add `IntentType.WIKI` to the coordinator
- `CoordinatorContext` gains: `wiki_action` (compile/list/show/export/status)
- Keyword recognition: "compile wiki", "update wiki", "wiki status", "show wiki page for X"

### Ingestion Integration (Hybrid Trigger)

When `wiki.compile_on_ingest=True`, `IngestionAgent.ingest_file()` creates a `WikiCompileJob(status=PENDING)` row after successful ingestion. This flags the document without blocking. Actual compilation happens on explicit `grimoire wiki compile` or background job.

Document states: `ingested` -> `wiki-pending` -> `wiki-compiled`

---

## Wiki Page Structure and Export

### Compiled Page Format

```markdown
# Authentication Pipeline

> Entity type: process | Version: 3 | Last compiled: 2026-04-20

## Overview

The authentication pipeline validates incoming requests using JWT tokens
issued by the auth service.

*Source: [auth-design.md](doc:abc123) | Priority: 10 | Compiled: 2026-04-20*

## Configuration

Server uses port 5432 for the auth service. Tokens expire after 24 hours.

*Source: [auth-config.yaml](doc:def456) | Priority: 7 | Compiled: 2026-04-20*

~~## Configuration (superseded 2026-04-18)~~

~~Server uses port 5434 for the auth service.~~
*Superseded by: section above | Source: [old-config.md](doc:ghi789)*

## Cross-References

- -> Depends on: [[Token Service]]
- -> Related to: [[API Gateway]], [[Session Management]]
```

Conventions:
- Each section ends with a source attribution line: source filename, document ID, priority, compile date
- Superseded sections are kept as strikethrough blocks with a pointer to the replacement
- Cross-references use `[[Page Title]]` wiki-link syntax
- Header block shows entity type, version, and last compile timestamp

### Export

`grimoire wiki export` writes files to configured `wiki_pages_dir`:

```
wiki/
  authentication-pipeline.md
  token-service.md
  api-gateway.md
  session-management.md
  _index.md              -- auto-generated index of all pages
```

`_index.md` lists every page with title, entity type, and a one-line summary.

### Reading vs Writing

The WikiAgent reads from WikiPageSection rows in the DB (source of truth for compilation). Exported markdown files are a read-only view for humans. The LLM never reads from exported files — it reads from the DB and raw source documents.

---

## Error Handling

| Scenario | Behavior |
|---|---|
| LLM unavailable during compile | Mark WikiCompileJob as FAILED with error message. Page untouched. Retry on next compile run. |
| LLM returns malformed entity extraction | Log warning, skip that entity. Continue with others. |
| No entities found in source | Mark job as COMPLETED with zero pages touched. Not an error. |
| Slug collision (two entities map to same slug) | Append disambiguator. LLM picks the better name. |
| Compile job interrupted mid-write | DB transaction wraps all section/cross-reference writes for a single document. Partial updates roll back. |
| Circular cross-reference | Allowed. Wiki pages can mutually reference each other. |

---

## Testing

| Layer | What to test |
|---|---|
| WikiPage / WikiPageSection models | CRUD, slug uniqueness, superseded-by chains, source_document_id FK |
| WikiAgent._identify_entities | Mock LLM: returns entities vs empty list vs malformed response |
| WikiAgent._match_existing_page | Exact slug match, no match, similar-but-not-exact match |
| WikiAgent._detect_contradictions | Mock LLM: factual, temporal, scope, terminology, no conflict |
| WikiAgent._apply_contradiction_policy | Newer-wins produces superseded section; source-priority with higher/lower/equal priority |
| WikiAgent.compile_document | New page creation, existing page update, multi-entity source, source with no entities |
| WikiAgent.compile_pending | Batch processing, partial failures, empty queue |
| CLI commands | compile, list, show, export, status |
| Coordinator routing | "compile the wiki" routes to WIKI intent |
| Export | Markdown format, _index.md generation, [[Title]] cross-reference syntax |
| Config | WikiConfig defaults, source_priorities from env vars and yaml |

LLM output quality is not unit-tested. We mock LLM calls and verify the agent processes mocked output correctly. Quality validation is manual review of exported wiki pages.