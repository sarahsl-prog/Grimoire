# Security usage — CLI & API recipes

This page collects practical CLI and HTTP recipes for the security domain.
For settings reference see [`configuration.md`](configuration.md); for the
underlying retriever math see [`retriever.md`](retriever.md).

## Switching on security mode

The single switch is `settings.security.domain`:

```bash
export GRIMOIRE_SECURITY__DOMAIN=security
```

or in `grimoire.yaml`:

```yaml
security:
  domain: security
```

The CLI and API factories pick up the new domain automatically — no
restart-the-coordinator dance.

## Security filter keys

The Phase 9 surface exposes a small fixed set of filters. They map to the
indexed columns added in Phase 2 (severity, source_type, cve_id,
mitre_technique_id, content_date) plus a few free-form fields that live in
the ``security_metadata`` JSON blob.

| Key | Type | Example |
|---|---|---|
| `severity` | string | `critical`, `high`, `medium`, `low`, `info` |
| `mitre_tactic` | string | `execution`, `lateral-movement` |
| `mitre_technique_id` | string | `T1059`, `T1059.001` |
| `source_type` | string | `sigma_rule`, `nvd_cve`, `mitre_attack`, `prose` |
| `cve_id` | string | `CVE-2024-12345` |
| `content_date_after` | ISO-8601 date | `2024-01-01` |
| `platforms` | list / CSV | `windows`, `linux`, `macos` |

Unknown keys are not rejected — the API and CLI emit a single log warning
and pass them through to the retriever, so newer clients keep working
against older servers. See `grimoire.api.schemas.SECURITY_FILTER_KEYS` for
the source-of-truth constant.

## CLI recipes

### Ask & search with filters

```bash
# Severity + MITRE tactic — typical SOC question.
grimoire ask --severity high --tactic execution "powershell"

# CVE lookup, focused on Sigma rules.
grimoire ask --cve-id CVE-2024-1234 --source-type sigma_rule "remote-code-execution coverage?"

# Search-only, multi-platform, time-bounded.
grimoire search --platform windows --platform linux \
                --content-date-after 2024-01-01 \
                "lateral movement"
```

All flags compose with the existing `--tag` / `--top-k` / `--no-cache`
options, and they apply to both `grimoire ask` and `grimoire search`.

### Ingest with autodetection override

```bash
# Force every file under ./rules to be parsed as Sigma, even if the
# extension is non-standard or the path lacks the usual hints.
grimoire ingest --source-type sigma_rule ./rules

# Ingest an NVD bulk JSON dump that lives outside /nvd-cve/.
grimoire ingest --source-type nvd_cve ./mirrors/nvd-2024.json
```

The override only affects security-domain chunking. When
`settings.security.domain == "general"` the flag is silently ignored.

## API recipes

### POST /query/ask

Security filters can be passed either as query parameters or in the body's
``filter_dict``. Body values win on conflicts.

```bash
curl -X POST 'http://localhost:8000/api/v1/query/ask?severity=high&tactic=execution' \
     -H 'content-type: application/json' \
     -H 'authorization: Bearer $GRIMOIRE_API_KEY' \
     -d '{"query": "powershell", "top_k": 5}'
```

Equivalent body-only form:

```bash
curl -X POST 'http://localhost:8000/api/v1/query/ask' \
     -H 'content-type: application/json' \
     -H 'authorization: Bearer $GRIMOIRE_API_KEY' \
     -d '{
       "query": "powershell",
       "top_k": 5,
       "filter_dict": {"severity": "high", "mitre_tactic": "execution"}
     }'
```

### POST /query/search

Same surface as `/ask`; returns ranked chunks without an LLM answer.

### GET /documents

List documents filtered by indexed security columns:

```bash
curl 'http://localhost:8000/api/v1/documents?source_type=sigma_rule&severity=critical' \
     -H 'authorization: Bearer $GRIMOIRE_API_KEY'
```

Supported list filters: `status`, `file_type`, `source_type`, `severity`,
`cve_id`, `mitre_technique_id`. All filters compose with AND semantics;
unsupplied filters are ignored.

## Notes & gotchas

* TLP enforcement is intentionally not wired (per the
  [security strategy plan](../plans/security_strategy_plan.md)). The
  ``tlp_level`` column exists for tooling but the API does not gate
  responses on it.
* Body ``filter_dict`` takes precedence over query-string shortcuts on
  ``/ask`` and ``/search`` — useful when a client wants to keep its filter
  payload in JSON while still passing the simple ``?severity=high``
  ergonomics for ad-hoc curl.
* The CLI `--platform` flag is repeatable (`--platform windows --platform
  linux`); the API uses a comma-separated string (`?platforms=windows,linux`).
* The `--source-type` Click choice for `grimoire ingest` is a closed set
  (`sigma_rule`, `nvd_cve`, `mitre_attack`, `ioc_list`, `prose`, `unknown`).
  Unknown values are rejected by Click before the agent runs.
