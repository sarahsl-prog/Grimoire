# Source-type detection

`SourceType` is the coarse-grained category Grimoire assigns to every piece of
ingested content in the security domain. It tells the downstream pipeline
which deterministic parser (Phase 3+) to dispatch to, and which fields to
expect in the resulting `SecurityMetadata` (Phase 2). The single entry point
is `grimoire.strategies.security.corpus.detect_source_type`, a pure function
that the upcoming `SecurityChunker` will call before deciding how to split a
document.

```python
from grimoire.strategies.security import SourceType, detect_source_type

source_type = detect_source_type(text, source_metadata={"path": path})
```

The function never raises and never performs I/O — it inspects only the text
it was handed plus an optional metadata mapping (currently the file `path` /
`source_path`).

## Rule precedence

Rules are checked in strict order; the **first match wins**. This determinism
is what makes the function safe to use as a dispatch key.

| # | Rule | Trigger | Result |
| --- | --- | --- | --- |
| 1 | Path hint — Sigma | `/sigma-rules/` or `/sigma/` substring in path | `SIGMA_RULE` |
| 1 | Path hint — NVD | `/nvd-cve/`, `/nvd/`, or `/cve/` substring in path | `NVD_CVE` |
| 1 | Path hint — MITRE | `/mitre-attack/`, `/attack/`, or `/mitre/` (excluding `/mitre-defend/`) | `MITRE_ATTACK` |
| 1 | Path hint — IOC | `/iocs/` or `/ioc-lists/` substring in path | `IOC_LIST` |
| 2 | Extension hint — Sigma | `.yml` / `.yaml` extension AND body contains both `detection:` and `logsource:` top-level keys | `SIGMA_RULE` |
| 3 | JSON shape — modern CVE | Parses as `{"cve": {"id": "CVE-YYYY-NNNN", ...}}` | `NVD_CVE` |
| 3 | JSON shape — legacy CVE | Top-level key matches `^CVE-\d{4}-\d+$` | `NVD_CVE` |
| 3 | JSON shape — bulk feed | `{"vulnerabilities": [{"cve": {"id": "CVE-..."}}, ...]}` | `NVD_CVE` |
| 3 | JSON shape — STIX bundle | `{"type": "bundle", "objects": [..., {"type": "attack-pattern", ...}]}` | `MITRE_ATTACK` |
| 4 | Markdown frontmatter | Body starts with `---\n` and frontmatter contains `kind: attack-pattern` or `attack_id: T<digits>` | `MITRE_ATTACK` |
| 5 | Filename hint | Basename matches `T1059`, `T1059.001`, `T1059.md`, `T1059.001.md` | `MITRE_ATTACK` |
| 6 | IOC sniff | Text under 64 KB and ≥80% of non-empty lines parse as IPv4, domain, MD5, SHA1, or SHA256 (with bracketed obfuscation tolerated) | `IOC_LIST` |
| 7 | Fallback — prose | Any non-trivial line longer than 60 characters | `PROSE` |
| 7 | Fallback — unknown | Empty or trivially short input that didn't match anything else | `UNKNOWN` |

## Examples

**Sigma rule via extension sniff**

```yaml
title: Suspicious cmd usage
logsource:
    product: windows
    service: security
detection:
    selection:
        EventID: 4688
    condition: selection
```

`detect_source_type(body, {"path": "/random/r.yml"})` → `SourceType.SIGMA_RULE`.

**NVD CVE via JSON shape**

```json
{"cve": {"id": "CVE-2024-12345", "descriptions": [{"lang": "en", "value": "..."}]}}
```

`detect_source_type(body)` → `SourceType.NVD_CVE`.

**MITRE ATT&CK via frontmatter**

```markdown
---
kind: attack-pattern
name: OS Credential Dumping
---
# OS Credential Dumping
...
```

`detect_source_type(body)` → `SourceType.MITRE_ATTACK`.

**IOC list via content sniff**

```
192.168.1.10
8.8.8.8
d41d8cd98f00b204e9800998ecf8427e
evil.example.com
```

`detect_source_type(body)` → `SourceType.IOC_LIST`. Note: detection of IOC
lists is supported now, but the dedicated **IOC chunker arrives in a later
phase** — until then IOC files will fall back to prose chunking even when
correctly classified as `IOC_LIST`.

## Why pure function, no LLM?

Source-type detection sits on the hot path of every ingestion job and needs
to run thousands of times per bulk feed. Anything LLM-shaped would dominate
total ingest cost, introduce non-determinism (the same file classified
differently across runs), and make the dispatch logic untestable. The rules
above were chosen so that:

- **All structured sources** (Sigma, NVD, MITRE) have at least two redundant
  signals — a path hint and a content shape — so misclassification only
  happens when *both* are absent.
- **Robustness is unconditional**: malformed JSON, broken YAML, and binary
  garbage never raise; they fall through to `PROSE` or `UNKNOWN`.
- **The full function runs in well under 50 ms** even on multi-MB CVE feeds
  (the IOC sniff is byte-capped at 64 KB and JSON parsing is gated on the
  text starting with `{` or `[`).

LLM-based metadata enrichment for the prose / unknown fallback arrives in
**Phase 6** (`SecurityMetadataExtractor`), where latency budget is large
enough to justify the call.

## Cross-references

- [Strategies README](./README.md) — package overview and phase status.
- [Security strategy plan](../plans/security_strategy_plan.md) — full roadmap,
  including the chunker dispatch (Phase 3) that consumes this detector.
