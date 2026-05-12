# Security retriever

The `SecurityRetriever` is the Phase 7 component that adds security-domain
re-ranking on top of Grimoire's existing hybrid search pipeline. It does **not**
replace `HybridSearch`; it composes it.

```
query ─► _classify_query ─► HybridSearch.search ─► _security_rerank ─► top_k
```

The wrapped `HybridSearch` still owns:

* dense vector retrieval against ChromaDB,
* full-text retrieval against the FTS5 mirror,
* reciprocal-rank fusion + cross-encoder re-rank.

`SecurityRetriever` only layers three multiplicative transforms on top of the
fused score so downstream consumers (`QueryAgent`, API, CLI) see the same
`list[HybridResult]` shape they already handle.

## Module location

`grimoire/strategies/security/retriever.py`

`SecurityRetriever` is also re-exported from `grimoire.strategies.security`,
loaded lazily to avoid a circular import with `grimoire.db.models`.

## API

```python
from grimoire.config.settings import get_settings
from grimoire.search.hybrid import HybridSearch
from grimoire.strategies.security import SecurityRetriever

retriever = SecurityRetriever(hybrid=HybridSearch(...), settings=get_settings())
results = await retriever.retrieve(
    db=session,
    query="CVE-2024-12345 RCE",
    top_k=10,
    filter_dict={"severity": "critical"},
)
```

`SecurityRetriever` subclasses
[`grimoire.strategies.base.BaseRetriever`](../../grimoire/strategies/base.py) and
satisfies its `async retrieve(db, query, *, top_k, filter_dict)` contract.

## Query intent classifier

`_classify_query(query)` returns one of four labels:

| Intent              | Trigger                                                                |
|---------------------|------------------------------------------------------------------------|
| `cve_lookup`        | full-string `CVE-YYYY-N+` match, or substring `cve-2024…` / `cve-2025…`|
| `technique_lookup`  | full-string `T1234` / `T1234.567`, or fragments of well-known IDs       |
| `ioc_lookup`        | valid IPv4 (octets 0–255), domain, MD5/SHA1/SHA256 hash, or fragment prefixes (`ip:`, `hash:`, `domain:`, RFC1918 prefixes) |
| `general_security`  | fallback for everything else (and empty / whitespace queries)          |

The classifier is regex-first, no LLM call on the hot path. Regex precedence
order: CVE > MITRE technique > IPv4 > domain > hash > fragments → fallback.

Future intents can be added by extending `QueryIntent` and the matching
patterns; the rerank matrix lives in `settings.security.intent_source_matrix`
so it picks up new labels without code changes.

## Re-ranking math

Each candidate score is multiplied by three factors:

```
score' = score × severity_weight × recency_multiplier × intent_source_multiplier
```

The transforms are commutative; the implementation applies them in a fixed
order (severity → recency → intent) only for readability.

### 1. Severity boost

```
severity_weight = settings.security.severity_weights[metadata.severity]
                  (defaults to 0.0 for "unknown" / missing)
```

Default weights:

| Severity   | Multiplier |
|------------|------------|
| `critical` | 3.0        |
| `high`     | 2.0        |
| `medium`   | 1.0        |
| `low`      | 0.5        |
| `info`     | 0.2        |
| `unknown`  | 0.0        |

The `unknown` weight of `0.0` is deliberate: it prunes documents that lack
structured severity metadata when a structured corpus is available, while
remaining configurable for prose-heavy deployments.

### 2. Recency decay

Exponential half-life model:

```
recency_multiplier = 0.5 ** (age_in_days / settings.security.recency_half_life_days)
```

* `recency_half_life_days = 365` (default) → one-year-old content scores 0.5×.
* `recency_half_life_days = 0` → recency disabled, multiplier is always 1.0.
* `content_date is None` → multiplier is 1.0 (don't penalize unknown ages).
* Future-dated content → multiplier 1.0 (no penalty).

ISO-8601 strings with a trailing `Z` are tolerated; malformed strings fall back
to "no recency adjustment" silently and never raise.

### 3. Intent-source alignment

A two-level dictionary keyed by intent → source type:

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

Missing intent rows fall back to `general_security`; missing source types
within a row default to `1.0` (neutral).

## Tunable settings

All knobs live under `settings.security`:

| Key                                  | Type                          | Default | Purpose                                              |
|--------------------------------------|-------------------------------|---------|------------------------------------------------------|
| `severity_weights`                   | `dict[str, float]`            | see above | severity multipliers                              |
| `recency_half_life_days`             | `int`                         | `365`   | recency decay half-life (0 disables)                |
| `intent_source_matrix`               | `dict[str, dict[str, float]]` | see above | intent → source-type boost                        |
| `llm_extract_enabled`                | `bool`                        | `False` | (Phase 6) opt-in LLM metadata extractor on ingest   |
| `domain`                             | `"general" \| "security"`     | `"general"` | switch the whole pipeline into security mode    |

There are no hard-coded magic numbers in the retriever — every weight is
sourced from settings and can be overridden by env var (`GRIMOIRE_SECURITY__…`)
or YAML config.

## Result handling

* `_security_rerank` mutates the input list in-place and preserves order
  (no sort). The caller is responsible for the final descending-score sort,
  which `retrieve()` does immediately before truncating to `top_k`.
* `retrieve()` over-fetches `top_k × 3` candidates from `HybridSearch` so the
  re-rank has enough material to reorder without the wrapped search starving
  the result set.
* Empty hybrid result → empty list (no error path, no NaN scores).

## Testing

See `tests/strategies/test_security_retriever.py` for:

* full classifier coverage (CVE, technique, IPv4 range bounds, domain,
  MD5/SHA1/SHA256, fragments, fallback, empty input);
* recency decay edge cases (`half_life=0`, `None` date, future date, exact
  half-life, two half-lives);
* combined severity + recency + intent ordering;
* settings-driven overrides (custom severity weights, custom intent matrix,
  unknown intent/source fallbacks, malformed content_date);
* `retrieve()` delegates to `HybridSearch.search` with the expected
  `top_k × 3` over-fetch and forwards `filter_dict`;
* empty hybrid result → empty list;
* the `BaseRetriever` ABC is satisfied.
