# Plan: Native Playbook Corpus (`SourceType.PLAYBOOK`)

**Status:** ✅ Implemented on branch `mcp-updates` (commits 57400aa → dd3840f)
**Author:** OpenCode session 2026-09-01
**Depends on:** security-domain pipeline (Phases 1–3); `grimoire_search_playbook` MCP tool (Option B, shipped in commit 9ba460b + d920d17)

## Context

`grimoire_search_playbook` currently answers over the Sigma rule corpus
(`source_type="sigma_rule"`). A true "playbook" search should also cover
response and mitigation playbooks — incident-response runbooks, ATT&CK
mitigation procedure docs, CISA response templates — which today fall through
to `prose` chunking and lose all structured metadata.

This plan adds a first-class `playbook` corpus type: detection, parsing,
structured metadata, chunking, search facets, and MCP/API/CLI surface.

## Current State (verified in code)

- `SourceType` enum (`grimoire/strategies/security/corpus.py:48-58`):
  `NVD_CVE`, `SIGMA_RULE`, `MITRE_ATTACK`, `IOC_LIST`, `PROSE`. No playbook type.
- `corpus.detect_source_type()` uses path hints (`_PATH_HINTS_*`), content
  sniffing (`detection:`/`logsource:` for Sigma), then falls back to `PROSE`.
- `security/chunker.py:126-133` dispatches `SIGMA_RULE`/`NVD_CVE`/`MITRE_ATTACK`;
  everything else → `_chunk_prose()`.
- `SecurityMetadata` (`security/metadata.py`) has detection/threat-intel fields
  but nothing playbook-specific (`action`, `trigger`, `phase`).
- Indexed SQL columns on `documents`: `source_type`, `cve_id`, `severity`,
  `mitre_technique_id` — all already generic enough for playbooks.
- MCP: `CveSearchInput`/`PlaybookSearchInput` + `_facet_search()` helper perform
  SQL pre-filtering + `document_id: {$in: ...}` vector restriction (reusable as-is).
- `SearchInput.source_type` / `docs list --source-type` plumbed through CLI → API.

## Goals

1. Ingest playbook documents (markdown/YAML) as `source_type="playbook"` with
   structured metadata.
2. Surface playbook facets (phase, action_type, severity) in SQL + Chroma metadata.
3. `grimoire_search_playbook` covers BOTH sigma_rule and playbook source types.
4. Full test coverage ≥ 80% on new/changed files.

## Non-Goals

- No new LLM extraction (metadata is parsed deterministically from front matter
  and section headers).
- No changes to the `SecurityRetriever` re-rank matrix beyond adding a
  playbook row (intent-source alignment).

## Implementation Tasks

### T1 — Source type + detection (`corpus.py`) — ~1 h
- Add `PLAYBOOK = "playbook"` to `SourceType`.
- Path hints: `/playbooks/`, `/runbooks/`, `/ir-playbooks/`, `/response-plans/`.
- Content sniff: markdown/YAML containing a `## Trigger` or `## Actions` section,
  or front matter keys `playbook:`/`phase:`/`severity:`.
- Tests: `tests/strategies/` corpus detection cases (positive + negative paths,
  multi-doc YAML, front-matter-only files).

### T2 — Parser (`parsers/playbook.py`) — ~2 h
- `parse_playbook(text) -> List[Tuple[str, SecurityMetadata]]`:
  - Split markdown on `## `/`### ` section headers (reuse regex approach from
    `chunker/markdown.py`).
  - Extract front matter (YAML) — title, phase, severity, mitre_technique_id,
    trigger conditions.
  - Emit one `(chunk_text, SecurityMetadata)` per major section; attach
    playbook fields into `SecurityMetadata` (see T3).
- Edge cases: empty file, front matter without sections, CRLF, nested headers.

### T3 — Metadata schema (`metadata.py`) — ~1 h
- Add optional fields to `SecurityMetadata`:
  `playbook_phase: Optional[str]` (identify/contain/eradicate/recover),
  `action_type: Optional[str]` (manual/automated),
  `trigger: Optional[str]`.
- Extend `to_db_columns()` — none of these need indexed columns; they ride in
  the JSONB blob.
- Extend `to_chromadb_metadata()` — emit `playbook_phase` (default `""`).
- Migration: **none required** — all new data lands in `security_metadata` JSONB;
  `source_type` column already stores the new tag. Only if we decide to index
  `playbook_phase` (not planned) would a migration be needed.

### T4 — Chunker dispatch (`chunker.py`) — ~1 h
- `_chunk_playbook()` using parse_playbook output; `chunk_type="playbook_section"`.
- Register in the source-type dispatch table.

### T5 — Search facets (MCP tools) — ~1 h
- `PlaybookSearchInput`: add `source_types: playbooks | sigma | all`
  (default `all` → `{"source_type": {"$in": ["playbook", "sigma_rule"]}}`),
  optional `phase` facet (JSONB contains via existing `_jsonb_list_contains` or,
  if indexed later, direct column).
- `_facet_search()` already takes `source_type: str`; generalize to accept
  `List[str]` in the SQL pre-select (`Document.source_type.in_(...)`).
- README tier table, CHANGELOG entry.

### T6 — API/CLI surface — ~1 h
- `SearchRequest.source_type` already accepts arbitrary strings — allowlist
  updated: add `"playbook"` in `cli/query.py:75`, `api/routes/query.py:83`,
  `cli/ingest.py:40`.
- No new REST endpoint needed (filtered search covers it).

### T7 — Fixtures, tests, migration smoke — ~2 h
- Fixtures: `tests/fixtures/security/playbooks/ransomware-containment.md`,
  `phishing-response.md`.
- Unit: parser (6–8 cases), corpus detection (4 cases), chunker dispatch (2),
  metadata serialization round-trip.
- MCP: extend `test_search_playbook_*` — mixed-corpus facet search, phase facet.
- Migration smoke (`tests/test_db_migrations.py`): confirm no-op (no schema change).

## Effort Estimate

| Task | Size | Notes |
|------|------|-------|
| T1 corpus detection | S | mirrors existing sigma/NVD rules |
| T2 parser | M | front matter + section split; reuses markdown helpers |
| T3 metadata fields | S | JSONB only; no migration |
| T4 chunker dispatch | S | one extra branch + handler |
| T5 MCP facets | S | generalize `_facet_search` to multi-source-type |
| T6 allowlists | S | three one-line list edits |
| T7 tests + fixtures | M | bulk of the effort |
| **Total** | **~1.5–2 days** including review | |

## Sequencing

T1 → T2 → T3 → T4 → T7 (tests through each phase) → T5/T6 → docs/CHANGELOG.

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Playbook formats vary wildly (no single standard like Sigma YAML) | Start with markdown conventions (front matter + sections); document the expected shape in `docs/strategies/source_types.md`; unknown shapes degrade to prose, not errors |
| `$in` on `document_id` gets slow with very large facet result sets | Cap pre-select at e.g. 500 doc ids (paginate); warn in tool output when truncated |
| SQLite dev DB `json_extract` behavior differences | Keep PortableJSON semantics; use SQLAlchemy `func.json_extract` (supported on both); test on sqlite3 + postgres |

## Validation Strategy

1. `uv run pytest tests/strategies tests/test_mcp.py -q` — all green.
2. `uv run coverage report` — new files ≥ 80%.
3. Smoke: ingest `tests/fixtures/security/playbooks/` on a dev DB, then
   `grimoire mcp` stdio → `grimoire_search_playbook` with `query='contain ransomware'`
   and `mitre_technique_id='T1486'` returns the fixture.
4. Update the migration smoke test to assert no schema diff (no alembic revision needed).

## Open Questions

1. **Playbook source data**: which corpus ships first — custom YAML front-matter
   playbooks, or adapting existing open-source IR runbooks (e.g.
   reagan-scholarship/attack-playbooks)? Corpus choice drives the parser edge cases.
2. **Index `playbook_phase`?** If phase filtering becomes hot, add an indexed
   column + alembic migration later; start with JSONB-only to keep the change
   schema-free.
3. **Should `grimoire_search_playbook` default to both types or keep
   sigma-only with an opt-in flag?** Defaulting to both is a behavior change to
   the tool we just shipped; consider a `source_types` param with default `all`
   called out in CHANGELOG.
