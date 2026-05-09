# Implementation Plan: Grimoire Security Strategy

**Date**: 2026-05-09
**Status**: Planned
**Related Issues**: None yet — create issues when starting individual phases.

---

## Decisions locked in

| Topic | Choice |
|---|---|
| Module layout | New `grimoire/strategies/` pkg; **reuse** existing async `Chunker` ABC + Pydantic `Chunk` (extended with `chunk_type` / `source_type`); `SecurityRetriever` **wraps** `HybridSearch` rather than reimplementing it |
| Metadata storage | DB columns on `documents` (indexed scalars) + JSONB blob, also serialized to Chroma metadata for vector-side filters |
| Source detection | Path/extension hints + lightweight content sniffing (deterministic) |
| Initial corpus | Sigma + NVD CVE + MITRE ATT&CK |
| Metadata extraction | Deterministic parsers for structured sources; LLM extractor for prose / unrecognized |
| Domain switch | `settings.security` block, env override `GRIMOIRE_SECURITY__DOMAIN=security` |
| Query filters | Reuse existing `filter_dict`; document supported keys; no TLP enforcement |
| Hetzner | Include compose tweaks, `.env.security.example`, deploy doc as final phase |

---

## Phase 0 — Branch + scaffolding (small)

**Goal:** Land an empty but importable package and a tracking doc, so subsequent PRs are small.

- Create `grimoire/strategies/__init__.py`, `grimoire/strategies/base.py`, `grimoire/strategies/security/__init__.py`.
- `base.py` defines:
  - `BaseChunker = grimoire.core.chunker.base.Chunker` (re-export, no parallel hierarchy).
  - `BaseRetriever` ABC with `async retrieve(db, query, *, top_k, filter_dict) -> list[HybridResult]` matching `HybridSearch.search` signature.
  - Strategy registry helper (`get_chunker_for(file_path, source_type)`).
- Extend `grimoire/core/chunker/base.py::Chunk` (Pydantic) with two optional fields: `chunk_type: str | None`, `source_type: str | None`. No behavior change for existing chunkers.

**Tests:** `tests/strategies/test_base.py` — imports succeed, registry round-trips, `Chunk` accepts and round-trips the new fields, existing chunker tests still pass.

**Docs:** Add `docs/strategies/README.md` introducing the package and the "general vs security" split. Update top-level `README.md` "Project layout" section.

---

## Phase 1 — Source-type detection (`corpus.py`)

**Goal:** A single deterministic function downstream code can rely on.

- `grimoire/strategies/security/corpus.py`:
  - `class SourceType(str, Enum)`: `nvd_cve`, `sigma_rule`, `mitre_attack`, `ioc_list`, `prose`, `unknown`.
  - `detect_source_type(text: str, source_metadata: dict) -> SourceType` using:
    - Path hints: `/sigma-rules/`, `/nvd-cve/`, `/mitre-attack/`.
    - Extension hints: `.yml`/`.yaml` + `detection:` key + `logsource:` → `sigma_rule`.
    - JSON shape: `cve.id` / top-level `CVE-ID` → `nvd_cve`.
    - Markdown frontmatter `kind: attack-pattern` or filename `T\d{4}` → `mitre_attack`.
  - Pure function, no LLM, no I/O.

**Tests:** `tests/strategies/test_corpus.py` — covers each positive case and at least one negative for each source type, plus the prose fallback. Use small inline fixtures, no large sample files.

**Docs:** `docs/strategies/source_types.md` — table of detection rules, examples of each.

---

## Phase 2 — Security metadata schema + Alembic migration

**Goal:** Persist structured security metadata so we can filter on it from SQL, FTS, and Chroma.

- `grimoire/strategies/security/metadata.py`:
  - Enums: `TLPLevel`, `Severity` (as in spec).
  - Pydantic model `SecurityMetadata` (rename from spec's dataclass for consistency with the rest of Grimoire). Fields per spec; `to_chromadb_metadata()` returns a flat dict (lists pipe-joined).
  - Indexed scalar columns added to `documents`:
    - `source_type: str | None` (indexed)
    - `cve_id: str | None` (indexed, sparse)
    - `severity: Enum | None` (indexed)
    - `mitre_technique_id: str | None` (indexed)
    - `content_date: datetime | None` (indexed)
    - `tlp_level: Enum | None` (default WHITE)
  - Wide-but-sparse fields stored in `documents.security_metadata: PortableJSON`.
- Alembic revision `0006_add_security_metadata.py` adding the columns and indexes. Idempotent for SQLite dev DB.
- Update `grimoire/db/models.py::Document` accordingly.
- Update Chroma metadata writer in `agents/ingestion.py::_embed_and_store` to merge `SecurityMetadata.to_chromadb_metadata()` per chunk when present.

**Tests:**
- `tests/test_db_models.py`: extend with security column round-trip.
- `tests/strategies/test_metadata.py`: `to_chromadb_metadata()` shape, enum serialization, `from_db_row()` reverse.
- Migration smoke test (sqlite): apply head, downgrade -1, re-apply.

**Docs:** `docs/strategies/metadata.md` — full field reference with example payloads for each source type. Update `docs/DESIGN.md` Section 3 (data model) with the new columns.

---

## Phase 3 — Sigma chunker + parser (simplest source)

**Goal:** Sigma rules ingested with one chunk per rule and full structured metadata.

- `grimoire/strategies/security/parsers/sigma.py`:
  - `parse_sigma(text) -> list[tuple[str, SecurityMetadata]]` (one entry per `---` separated YAML doc).
  - Reads `title`, `id`, `level → severity`, `tags` → `mitre_technique_id`, `mitre_tactic`, `logsource` → `platforms`/`log_sources`, `detection.condition`, `falsepositives` → `detection_categories`.
- `grimoire/strategies/security/chunker.py`:
  - `class SecurityChunker(Chunker)` (subclass of existing async ABC).
  - `async chunk(text, doc_id=None)` calls `detect_source_type`, dispatches to `_chunk_sigma`, `_chunk_cve`, `_chunk_mitre`, `_chunk_prose` (last just delegates to `RecursiveCharacterTextSplitter`).
  - `_chunk_sigma` uses `parse_sigma`, builds one `Chunk` per rule with `chunk_type="sigma_rule"`, populates Chunk.metadata with rule's `SecurityMetadata.to_chromadb_metadata()` plus `chunk_id`.
  - Continuity links set via inherited `_set_continuity_links`.

**Tests:**
- `tests/strategies/test_sigma_parser.py` — parses a multi-doc Sigma sample, validates extracted metadata.
- `tests/strategies/test_security_chunker.py` (Sigma path) — check chunk count, `chunk_type`, no field bleed across rules.
- Fixture: `tests/fixtures/security/sigma/sample_rules.yml` (3–4 small rules covering Windows + Linux).

**Docs:** Section in `docs/strategies/chunking.md` describing Sigma rule chunking. Add a sample rule + resulting chunk to illustrate.

---

## Phase 4 — NVD CVE chunker + parser

- `grimoire/strategies/security/parsers/nvd.py`:
  - Handles NVD JSON 2.0 schema (CVE record + CVSS metrics + references).
  - `parse_cve(record: dict) -> tuple[str, SecurityMetadata]`: extracts `cve_id`, `cvss_score`, `severity` (mapped from baseSeverity), `cwe_ids`, `affected_products` (`configurations.cpeMatch` cpe23Uri product names), `published_date`, `description (en)`.
- `chunker._chunk_cve`:
  - Two chunks per CVE: (a) description + CVSS summary + severity + CWE list, (b) references list. Both share the same `SecurityMetadata`.
  - For bulk JSON (file containing many CVEs), iterate and emit chunks for each.
- Update `agents/ingestion.py::_select_chunking_strategy` and `_create_chunker` so JSON files routed through `SecurityChunker` when domain=security or detected source_type is `nvd_cve`.

**Tests:**
- `tests/strategies/test_nvd_parser.py` — synthetic CVE with all CVSS variants (v2, v3.0, v3.1).
- `tests/strategies/test_security_chunker.py` (CVE path).
- Fixture: small `nvdcve-sample.json` (3 CVEs spanning severities).

**Docs:** Append CVE section to `docs/strategies/chunking.md`. Add an "Ingesting NVD bulk feeds" note pointing at `nvd.nist.gov` annual download URLs.

---

## Phase 5 — MITRE ATT&CK chunker + parser

- `grimoire/strategies/security/parsers/mitre.py`:
  - Two input flavors: STIX 2.1 JSON (`attack-pattern` objects) and the markdown export with frontmatter.
  - Extracts `mitre_technique_id`, `mitre_tactic` (kill-chain phases), `mitre_subtechnique`, `platforms`, `description`, `procedure_examples`, `mitigations`.
- `chunker._chunk_mitre`:
  - One chunk per H2 section (Description / Procedure Examples / Mitigations / Detection). All chunks of one technique share `mitre_technique_id` so re-rank can group.

**Tests:**
- `tests/strategies/test_mitre_parser.py` — STIX and markdown variants.
- Chunker test verifies section split count and shared technique ID.
- Fixture: one technique in each format.

**Docs:** ATT&CK section in `docs/strategies/chunking.md`. Note re-export from upstream repo (`mitre/cti`).

---

## Phase 6 — Prose fallback + LLM metadata extractor

**Goal:** Catch HTB notes / blog posts / unrecognized files.

- `chunker._chunk_prose`: delegate to existing `RecursiveCharacterTextSplitter` but stamp `chunk_type="prose"`, `source_type="prose"` and an empty `SecurityMetadata`.
- `grimoire/strategies/security/extractor.py`:
  - `class SecurityMetadataExtractor` parallel to `core/tagger.py::Tagger`. Reuses Ollama settings (`settings.llm`) for the call.
  - JSON-mode prompt asks for: severity, mitre_technique_id (if mentioned), threat_actors, malware_families, platforms, ioc_types, content_date.
  - Strict pydantic validation on the response; on parse failure log + return empty `SecurityMetadata` (never block ingest).
- Plug extractor into `agents/ingestion.py` ingestion flow:
  - After parse, before chunk: if `source_type in {prose, unknown}` and `settings.security.llm_extract_enabled`, run extractor; merge result into doc-level metadata; pass to chunker so chunks inherit it.
  - For structured sources, parser already supplies metadata; skip the LLM call.

**Tests:**
- `tests/strategies/test_metadata_extractor.py` with mocked Ollama responses (success, malformed JSON, timeout, partial fields).
- Integration test in `tests/test_ingestion_agent.py`: prose doc with mock extractor → SecurityMetadata persisted to DB and Chroma.

**Docs:** `docs/strategies/extractor.md` — prompt, JSON schema, failure modes, cost guidance.

---

## Phase 7 — SecurityRetriever (re-rank wrapper)

**Goal:** Same hybrid pipeline, post-fusion re-rank using security signals.

- `grimoire/strategies/security/retriever.py`:
  - `class SecurityRetriever(BaseRetriever)` composes `HybridSearch` (vector + FTS + cross-encoder rerank already in place).
  - Workflow: `intent = _classify_query(query)` → `merged = await hybrid.search(...)` → `_security_rerank(merged, intent)` → top_k.
  - `_classify_query`: regex first (`CVE-\d{4}-\d+` → `cve_lookup`, `T\d{4}(\.\d{3})?` → `technique_lookup`, IOC regexes for IP/domain/hash → `ioc_lookup`, else `general_security`).
  - `_security_rerank`: severity boost, recency exponential decay (half-life from `settings.security.recency_half_life_days`), intent-source alignment matrix (configurable). Boost factors live in settings, not hard-coded.
  - Always returns `list[HybridResult]` so downstream `QueryAgent` is unchanged.
- Settings: add `SecurityConfig(BaseModel)` with `domain`, `severity_weights`, `recency_half_life_days`, `intent_source_matrix`, `llm_extract_enabled`. Wire into `GrimoireSettings.security`.

**Tests:**
- `tests/strategies/test_security_retriever.py`:
  - Classifier coverage on synthetic queries.
  - Re-rank ordering: given two stub HybridResults, severity=critical with old date vs severity=low with new date — assert deterministic rule.
  - `recency_half_life_days=0` disables decay (regression guard).
  - Intent matrix override via settings reorders results.
- Mock `HybridSearch.search` to return canned hits; no real DB/Chroma needed.

**Docs:** `docs/strategies/retriever.md` — classifier rules, reranking math, tunable weights.

---

## Phase 8 — Strategy loader + ingestion/query wiring

**Goal:** Single place that selects the right strategy based on `settings.security.domain`.

- `grimoire/strategies/loader.py`:
  - `def load_chunker(settings) -> Chunker` and `def load_retriever(settings, hybrid_search) -> BaseRetriever`.
  - Returns `SecurityChunker` / `SecurityRetriever` when `settings.security.domain == "security"`, else default factories (existing behavior).
- `agents/ingestion.py::_create_chunker`:
  - When `settings.security.domain == "security"`: always use `SecurityChunker` (it internally dispatches).
  - Otherwise: existing per-extension logic.
  - Respect `settings.security.domain == "general"` even on security-tagged paths so a homelab instance behaves identically to today.
- `agents/query.py`: accept `retriever: BaseRetriever | None = None`, fall back to `HybridSearch` if `None`. `QueryAgent.search` calls `await retriever.retrieve(...)` instead of `hybrid.search(...)` when present.
- Coordinator/factories (`agents/coordinator.py`, dependency injection in `api/dependencies.py`) updated to pass the loader's outputs.

**Tests:**
- `tests/strategies/test_loader.py` — both domains return the right classes; missing config defaults to general.
- Update `tests/test_ingestion_agent.py` and `tests/test_query_agent.py` with one parametrized case each for `domain=security`.

**Docs:** Update `docs/IMPLEMENTATION.md` with the strategy loader hook. Add `docs/strategies/configuration.md` listing all `settings.security.*` fields.

---

## Phase 9 — API + CLI surface

**Goal:** Make the new metadata filterable from public surfaces. No new endpoints; reuse what exists.

- `grimoire/api/schemas.py::QueryRequest.filter_dict` — document the security keys (severity, mitre_tactic, mitre_technique_id, source_type, cve_id, content_date_after, platforms). Add a `SECURITY_FILTER_KEYS` constant for validation (warning on unknown keys, not rejection — forward-compat).
- `grimoire/api/routes/query.py` and `routes/documents.py` (list endpoint): accept the same filters as query params and translate to `filter_dict`.
- `grimoire/cli/query.py` and `cli/ingest.py`:
  - `grimoire query --severity high --tactic execution "powershell"` etc.
  - `grimoire ingest --source-type sigma_rule ./rules` to override autodetection when needed.
- No auth changes (TLP not enforced).

**Tests:**
- `tests/test_api.py`: filtered query end-to-end against an in-memory pipeline (mocked retriever).
- `tests/test_cli.py`: new flags parse and propagate; unknown filter keys surface a warning, not an error.

**Docs:** Update `docs/IMPLEMENTATION.md` API section with filter examples; add `docs/strategies/usage.md` showing CLI/API recipes.

---

## Phase 10 — Hetzner deploy artifacts + docs

**Goal:** A homelab user with a Hetzner box can `git clone && cp .env.security.example .env && docker compose up`.

- `.env.security.example` — `GRIMOIRE_SECURITY__DOMAIN=security`, `POSTGRES_PORT=5433`, `CHROMADB_COLLECTION=security_grimoire`, sane Ollama cloud / HF embedding endpoints, no secrets committed.
- `docker-compose.security.yml` — overlay extending `docker-compose.yml`: forces `GRIMOIRE_SECURITY__DOMAIN`, mounts `/security-corpus`, opens only Postgres+Redis+Chroma (no Ollama container — cloud).
- `scripts/security/seed_corpus.sh` — checks out MITRE ATT&CK + SigmaHQ to `/security-corpus`, downloads NVD annual JSON.
- `docs/deploy/hetzner_security.md` — sizing (CPX21 / CPX31), one-shot bootstrap, firewall rules, backup of Postgres + Chroma volume, log location, smoke-test queries.
- Update `README.md` with a one-paragraph "Security mode" section linking to the deploy doc.

**Tests:**
- `tests/deploy/test_compose_overlay.py` — parses both compose files, asserts merged config has expected env vars and volumes.
- `bash -n scripts/security/seed_corpus.sh` syntax check (no execution in CI).

**Docs:** Already listed; final doc pass to make sure cross-links between `docs/strategies/*.md` and `docs/deploy/hetzner_security.md` exist.

---

## After every phase

Each phase ends with:

1. `uv run pytest -q tests/strategies tests/test_<touched>.py` — green.
2. `uv run ruff check . && uv run ruff format .` — clean.
3. Doc updates committed alongside the code (no doc-debt tail).
4. Single commit per phase on `claude/plan-grimoire-implementation-eHgYp`, push, draft PR updated with phase checklist.

## Out of scope (explicitly)

- D3FEND, IOC list parsers, HTB note ingestion (post-v1).
- TLP-level enforcement tied to API keys.
- Replacement of `HybridSearch`'s reranker — `SecurityRetriever` composes, doesn't replace.
- Re-embedding existing documents on the schema migration; new columns are nullable and back-fill is a separate operational task.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Migration breaks SQLite dev DB | Use `op.batch_alter_table` for the column adds; smoke-test in Phase 2. |
| LLM extractor latency dominates ingest | Off by default for structured sources; opt-in via `settings.security.llm_extract_enabled`. |
| Chroma metadata size limits | `to_chromadb_metadata()` only emits scalars + small pipe-joined lists; cap list lengths at 32 entries. |
| Re-rank weights tuned for one corpus, wrong for another | All weights live in `settings.security.*`, no hard-coded magic numbers. |
| Source-type misdetection silently degrades retrieval | `--source-type` CLI override + log a warning at INFO when fallback `prose` is selected for files in `/sigma-rules/` etc. |
