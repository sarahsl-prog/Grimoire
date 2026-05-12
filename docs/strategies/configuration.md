# Security strategy configuration

All security-domain knobs live under ``settings.security``. The Phase 8
strategy loader uses ``settings.security.domain`` as the single switch
between Grimoire's general pipeline and the security pipeline; everything
else under ``settings.security`` is consumed by the ingestion chunker, the
LLM metadata extractor, or the retriever.

## Field reference

| Key | Type | Default | Used by | Purpose |
|---|---|---|---|---|
| `security.domain` | `"general" \| "security"` | `"general"` | Phase 8 loader | Flip the whole ingestion/query pipeline into security mode. |
| `security.llm_extract_enabled` | `bool` | `False` | Phase 6 extractor | Run the LLM metadata extractor on prose / unknown documents during ingest. Off by default to keep ingest fast. |
| `security.severity_weights` | `dict[str, float]` | `{critical: 3.0, high: 2.0, medium: 1.0, low: 0.5, info: 0.2, unknown: 0.0}` | `SecurityRetriever` | Multiplier per severity in re-rank. `unknown=0.0` prunes documents that lack structured severity metadata. |
| `security.recency_half_life_days` | `int` | `365` | `SecurityRetriever` | Days for a 50 % score decay. `0` disables recency entirely. |
| `security.intent_source_matrix` | `dict[str, dict[str, float]]` | see below | `SecurityRetriever` | Maps query intent → per-source-type boost. |

### Default `intent_source_matrix`

```yaml
intent_source_matrix:
  cve_lookup:
    nvd_cve: 2.0
    sigma_rule: 1.0
    mitre_attack: 0.5
    prose: 0.2
  technique_lookup:
    mitre_attack: 2.0
    sigma_rule: 1.0
    nvd_cve: 0.5
    prose: 0.2
  ioc_lookup:
    sigma_rule: 1.5
    prose: 0.5
    nvd_cve: 0.5
    mitre_attack: 0.3
  general_security:
    sigma_rule: 1.0
    nvd_cve: 1.0
    mitre_attack: 1.0
    prose: 1.0
```

Missing intent rows fall back to the `general_security` row; missing
source-types within a row default to `1.0` (neutral).

## How the loader uses these fields

1. `grimoire.strategies.loader.load_chunker(settings, chunk_config=...)`
   returns a `SecurityChunker` when `settings.security.domain == "security"`,
   else `None`. The general pipeline keeps its per-extension chunker logic.
2. `grimoire.strategies.loader.load_retriever(settings, hybrid_search)`
   returns a `SecurityRetriever` wrapping the supplied `HybridSearch` when
   in security mode, else `None`. `QueryAgent` calls the retriever
   transparently if one is wired; otherwise it uses `HybridSearch.search`
   directly.

The loader is intentionally permissive on input: a missing `security`
block, an empty `domain` string, or an unrecognised domain value all
resolve to "general" (return `None`). This keeps older settings instances
(e.g. YAML predating Phase 7) working without a migration step.

## Setting values

### Environment variables

The standard `GRIMOIRE_<SECTION>__<KEY>` env-var format applies:

```bash
export GRIMOIRE_SECURITY__DOMAIN=security
export GRIMOIRE_SECURITY__LLM_EXTRACT_ENABLED=true
export GRIMOIRE_SECURITY__RECENCY_HALF_LIFE_DAYS=180
```

Nested dict fields (`severity_weights`, `intent_source_matrix`) are best
configured via YAML — env-var support for nested dicts is fiddly.

### YAML (`grimoire.yaml`)

```yaml
security:
  domain: security
  llm_extract_enabled: true
  recency_half_life_days: 180
  severity_weights:
    critical: 4.0
    high: 2.5
    medium: 1.0
    low: 0.3
    info: 0.1
    unknown: 0.0
  intent_source_matrix:
    cve_lookup:
      nvd_cve: 3.0
      sigma_rule: 1.5
      mitre_attack: 0.5
      prose: 0.1
```

## Backwards compatibility

* Settings that omit the entire `security` block continue to work — the
  loader treats them as `domain="general"` and returns `None`.
* Agent classes accept `settings=None` (`IngestionAgent`) /
  `retriever=None` (`QueryAgent`) and fall back to the pre-Phase-8 paths
  in that case, so callers that have not threaded settings through are
  unaffected.

## Related documents

* [`README.md`](README.md) — high-level overview of the security strategy.
* [`source_types.md`](source_types.md) — how detection picks `source_type`.
* [`metadata.md`](metadata.md) — `SecurityMetadata` field reference.
* [`chunking.md`](chunking.md) — chunker behaviour per source type.
* [`extractor.md`](extractor.md) — Phase 6 LLM metadata extractor.
* [`retriever.md`](retriever.md) — Phase 7 `SecurityRetriever`.
