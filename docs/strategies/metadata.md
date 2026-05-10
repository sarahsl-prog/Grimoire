# Security metadata

`SecurityMetadata` is the structured envelope Grimoire attaches to every
ingested security-domain document so that downstream code can filter,
join, and re-rank using fields like CVE id, CVSS score, ATT&CK technique,
TLP level, threat actor, and content date. It is defined in
`grimoire.strategies.security.metadata` and persisted across three sinks:

1. **Indexed scalar columns on `documents`** — for fast SQL filters and
   joins. Phase 2 adds: `source_type`, `cve_id`, `severity`,
   `mitre_technique_id`, `tlp_level`, `content_date`.
2. **`documents.security_metadata` JSONB blob** — wide but sparse storage
   for everything else (lists, the full SecurityMetadata payload).
3. **ChromaDB metadata** — flat dict of scalars + pipe-joined strings,
   produced by `SecurityMetadata.to_chromadb_metadata()`. Vector queries
   can post-filter on these keys.

Phase 2 only plumbs the schema and persistence. Population by parsers and
the LLM extractor lands in Phases 3+; cf. the
[security strategy plan](../plans/security_strategy_plan.md).

## Field reference

| Field | Type | Default | Description | Indexed? |
| --- | --- | --- | --- | --- |
| `source_type` | `SourceType` | `UNKNOWN` | Coarse-grained source category from `corpus.detect_source_type`. | ✅ (`ix_documents_source_type`, `ix_documents_source_type_severity`) |
| `source_url` | `str \| None` | `None` | Canonical upstream URL (NVD page, ATT&CK page, etc.). | — |
| `tlp_level` | `TLPLevel` | `WHITE` | Traffic Light Protocol level. Persisted as enum column. | — (column present, no dedicated index) |
| `cve_id` | `str \| None` | `None` | Validated against `^CVE-\d{4}-\d+$`. | ✅ (`ix_documents_cve_id`) |
| `cvss_score` | `float \| None` | `None` | Bounded `[0.0, 10.0]`. Stored in JSONB blob. | — |
| `severity` | `Severity` | `UNKNOWN` | Coarse bucket: critical/high/medium/low/info/unknown. | ✅ (`ix_documents_severity`, composite indexes) |
| `cwe_ids` | `list[str]` | `[]` | E.g. `["CWE-79", "CWE-89"]`. Pipe-joined in ChromaDB. | — (JSONB) |
| `affected_products` | `list[str]` | `[]` | CPE strings or human-readable product names. | — (JSONB) |
| `published_date` | `datetime \| None` | `None` | Upstream publication date. Naive → UTC. | — (JSONB) |
| `mitre_technique_id` | `str \| None` | `None` | Validated against `^T\d{4}(\.\d{3})?$`. | ✅ (`ix_documents_mitre_technique_id`) |
| `mitre_tactic` | `str \| None` | `None` | E.g. `"execution"`, `"persistence"`. | — |
| `mitre_subtechnique` | `str \| None` | `None` | Human-readable sub-technique name. | — |
| `threat_actors` | `list[str]` | `[]` | APT / actor names. | — (JSONB) |
| `malware_families` | `list[str]` | `[]` | Malware families referenced. | — (JSONB) |
| `ioc_types` | `list[str]` | `[]` | IOC types present (`"ipv4"`, `"domain"`, `"sha256"`). | — (JSONB) |
| `detection_categories` | `list[str]` | `[]` | Sigma `falsepositives` notes / detection categories. | — (JSONB) |
| `platforms` | `list[str]` | `[]` | Platform tags (`"windows"`, `"linux"`, `"aws"`). | — (JSONB) |
| `log_sources` | `list[str]` | `[]` | Sigma `logsource` channels. | — (JSONB) |
| `content_date` | `datetime \| None` | `None` | Effective date of the content (used by recency re-rank in Phase 7). Naive → UTC. | ✅ (`ix_documents_content_date`, `ix_documents_severity_content_date`) |

`source_type`, `severity`, and `tlp_level` always have a non-null value
(their defaults). All other fields default to `None` / `[]` and are only
serialised when set. Datetime fields normalise naive inputs to UTC; TZ-
aware inputs pass through unchanged.

## ChromaDB serialization

ChromaDB metadata can only store scalars (`str`, `int`, `float`, `bool`).
`SecurityMetadata.to_chromadb_metadata()` therefore:

- pipe-joins list fields (`"CWE-79|CWE-89"`),
- caps each list at **32 entries** (excess is silently truncated; a
  debug log records the discard),
- ISO-8601 serialises datetimes,
- substitutes `""` for unset text fields, `0.0` for unset
  `cvss_score`, and `"unknown"` / `"white"` for the always-present
  `severity` / `tlp_level` defaults,
- always emits the keys: `source_type`, `severity`, `tlp_level`,
  `cve_id`, `cvss_score`, `cwe_ids`, `mitre_technique_id`,
  `mitre_tactic`, `threat_actors`, `platforms`, `content_date`,
  `source_url`.

Filters on the vector store therefore look like:

```python
filter_dict = {
    "source_type": "nvd_cve",
    "severity": {"$in": ["critical", "high"]},
}
results = await hybrid.search(query, filter_dict=filter_dict)
```

## Example payloads

### NVD CVE

```python
SecurityMetadata(
    source_type=SourceType.NVD_CVE,
    cve_id="CVE-2024-12345",
    cvss_score=9.8,
    severity=Severity.CRITICAL,
    cwe_ids=["CWE-79", "CWE-89"],
    affected_products=["cpe:2.3:a:acme:widget:1.0:*:*:*:*:*:*:*"],
    published_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
    source_url="https://nvd.nist.gov/vuln/detail/CVE-2024-12345",
    content_date=datetime(2024, 1, 15, tzinfo=timezone.utc),
)
```

### Sigma rule

```python
SecurityMetadata(
    source_type=SourceType.SIGMA_RULE,
    severity=Severity.HIGH,
    mitre_technique_id="T1059.001",
    mitre_tactic="execution",
    detection_categories=["false_positive_admin_scripts"],
    platforms=["windows"],
    log_sources=["windows-security"],
)
```

### MITRE ATT&CK technique

```python
SecurityMetadata(
    source_type=SourceType.MITRE_ATTACK,
    mitre_technique_id="T1059",
    mitre_tactic="execution",
    platforms=["windows", "linux", "macos"],
    source_url="https://attack.mitre.org/techniques/T1059/",
)
```

### Prose / unrecognized

```python
SecurityMetadata()  # all defaults; fields populated by Phase 6 LLM extractor
```

## Round-tripping

`SecurityMetadata.from_db_row(doc)` reconstructs an instance from a
SQLAlchemy `Document` row by reading the indexed scalars and merging
the `documents.security_metadata` JSONB blob on top. String enum values
are coerced back to their enum types so the resulting instance is fully
typed.

The ingestion agent's `_apply_security_metadata` helper (in
`grimoire/agents/ingestion.py`) calls both `to_db_columns()` and
`model_dump(mode="json")` to populate the indexed columns and the JSONB
blob in lock-step. Phase 2 wires the helper but no caller passes
metadata yet — that arrives in Phase 3.

## Cross-links

- [Security strategy plan](../plans/security_strategy_plan.md) — the
  full multi-phase roadmap.
- [Source types](source_types.md) — Phase 1 detection rules that drive
  `SecurityMetadata.source_type`.
- [Strategies README](README.md) — package overview and phase status.
