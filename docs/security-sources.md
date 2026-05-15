# Grimoire Security Data Sources

> Generated: 2026-05-12

---

## Primary Sources (Already Ingested)

### MITRE ATT&CK (Enterprise)

| Field | Value |
|-------|-------|
| **URL** | https://github.com/mitre/cti |
| **Download** | `https://raw.githubusercontent.com/mitre/cti/master/enterprise-attack/enterprise-attack.json` |
| **Format** | STIX 2.1 JSON |
| **Size** | ~60 MB |
| **Update cadence** | Monthly — patches released with ATT&CK version increments (every 1-2 months) |
| **Incremental?** | No. Full bundle replacement. GitHub tags let you diff versions. |
| **Grimoire parser** | `grimoire/strategies/security/parsers/mitre.py` — handles STIX `attack-pattern` objects |

**Recommended ingest schedule:** Download monthly. Check GitHub tags or releases for new versions. Full re-ingest on version change.

---

### NVD CVE

| Field | Value |
|-------|-------|
| **URL** | https://nvd.nist.gov/vuln/data-feeds |
| **API** | `https://services.nvd.nist.gov/rest/json/cves/2.0` |
| **Format** | JSON (API) or JSON gzip (bulk feeds) |
| **Size** | ~150 MB compressed (full corpus); increments ~2-5 MB/day |
| **Update cadence** | Continuous — NVD adds/updates CVEs throughout the day. Official "modified" feeds published hourly. |
| **Incremental?** | Yes. API supports `lastModStartDate`/`lastModEndDate` parameters for delta queries. The `cves/2.0` API also supports pagination. Bulk feeds are split by year. |
| **Grimoire parser** | `grimoire/strategies/security/parsers/nvd.py` — handles NVD JSON wrapper, extracts `cve_description` + `cve_references` chunks |

**Recommended ingest schedule:** Daily incremental (query `lastModStartDate=yesterday`). Full re-sync quarterly.

**Scripting notes:**
```bash
# Delta query — CVEs modified in the last 24 hours
curl "https://services.nvd.nist.gov/rest/json/cves/2.0?lastModStartDate=$(date -d '-1 day' +%Y-%m-%dT00:00:00.000)&lastModEndDate=$(date +%Y-%m-%dT00:00:00.000)"
```
NVD API requires an API key for reasonable rate limits (50 requests/30s with key, 5 without). Free key at https://nvd.nist.gov/developers/request-an-api-key

---

## High-Value Sources (Recommended Next)

### CISA KEV (Known Exploited Vulnerabilities)

| Field | Value |
|-------|-------|
| **URL** | https://www.cisa.gov/known-exploited-vulnerabilities-catalog |
| **Download** | `https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json` |
| **Format** | JSON |
| **Size** | ~500 KB |
| **Update cadence** | Weekly — typically Tuesdays/Thursdays |
| **Incremental?** | No. Full catalog replacement. Small enough that re-downloading is trivial. |
| **Grimoire parser** | Not yet implemented — straightforward JSON, similar to NVD structure |

**Recommended ingest schedule:** Daily download. Diffs are tiny; just re-ingest the whole thing.

**Scripting notes:**
```bash
curl -L -o cisa-kev.json \
  https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json
```
Supports conditional requests (ETag/Last-Modified). If HTTP 304, skip re-ingest.

---

### Sigma Rules (Full Catalog)

| Field | Value |
|-------|-------|
| **URL** | https://github.com/SigmaHQ/sigma |
| **Download** | `git clone https://github.com/SigmaHQ/sigma.git` or download specific rule trees |
| **Format** | YAML (detection rules) |
| **Size** | ~25 MB (full repo); ~3,000+ rule files |
| **Update cadence** | Multiple times per week — community-driven, PRs merged regularly |
| **Incremental?** | Yes — `git pull` on the cloned repo. Only changed files since last pull. |
| **Grimoire parser** | `grimoire/strategies/security/parsers/sigma.py` — already handles Sigma YAML |

**Recommended ingest schedule:** Weekly `git pull && re-ingest changed files`. Don't re-ingest the whole repo every time — only process files with new/modified git blobs.

**Scripting notes:**
```bash
cd sigma && git pull origin main
# Find changed files since last ingest
git diff --name-only HEAD~1 -- rules/  # or track a ref in a .last_ingest file
```

---

### MITRE CAPEC (Common Attack Pattern Enumeration)

| Field | Value |
|-------|-------|
| **URL** | https://capec.mitre.org/ |
| **Download** | `https://raw.githubusercontent.com/mitre/cti/master/capec/stix/capec.json` |
| **Format** | STIX 2.1 JSON |
| **Size** | ~5 MB |
| **Update cadence** | ~Quarterly (with major CAPEC version releases) |
| **Incremental?** | No. Full bundle replacement. |
| **Grimoire parser** | Not yet implemented — similar structure to ATT&CK STIX, would reuse patterns from MITRE parser |

**Recommended ingest schedule:** Quarterly. Check GitHub tags for new versions.

---

## Medium-Value Sources

### OWASP Top 10 & Cheat Sheet Series

| Field | Value |
|-------|-------|
| **URL** | https://owasp.org/www-project-top-ten/ and https://github.com/OWASP/CheatSheetSeries |
| **Format** | Markdown (cheat sheets) |
| **Size** | ~5 MB (cheat sheet repo) |
| **Update cadence** | Irregular — Top 10 updates every 3-4 years; cheat sheets monthly |
| **Incremental?** | Git repo — `git pull` for deltas |
| **Grimoire parser** | Standard Markdown chunker (already exists) |

**Recommended ingest schedule:** Monthly `git pull`. Content is relatively stable; won't change much week-to-week.

---

### MITRE D3FEND (Defensive Techniques)

| Field | Value |
|-------|-------|
| **URL** | https://d3fend.mitre.org/ |
| **Download** | `https://d3fend.mitre.org/ontologies/d3fend.json` or via the GitHub release at https://github.com/D3FEND/d3fend-ontology |
| **Format** | JSON-LD / OWL ontology |
| **Size** | ~15 MB |
| **Update cadence** | ~Quarterly |
| **Incremental?** | No. Full ontology replacement. |
| **Grimoire parser** | Not yet implemented — would need a new ontology-to-chunk adapter |

**Recommended ingest schedule:** Quarterly. D3FEND is valuable for mapping ATT&CK techniques to defensive countermeasures.

---

### NIST SP 800-53 (Security Controls)

| Field | Value |
|-------|-------|
| **URL** | https://csrc.nist.gov/publications/detail/sp/800-53/rev-5/final |
| **Download** | https://csrc.nist.gov/CSRC/media/Publications/sp/800-53/rev-5/final/documents/sp800-53r5-control-catalog.xlsx |
| **Format** | XLSX (control catalog) and PDF (full publication) |
| **Size** | XLSX ~1 MB; PDF ~5 MB |
| **Update cadence** | Rare — Rev 5 is current; updates are minor errata |
| **Incremental?** | No. Full replacement when a revision drops. |
| **Grimoire parser** | Not yet implemented — would need XLSX/PDF extraction |

**Recommended ingest schedule:** On-demand. Content barely changes (major revision every 5+ years).

---

## Lower-Priority Sources

### Exploit-DB

| Field | Value |
|-------|-------|
| **URL** | https://www.exploit-db.com/ |
| **Download** | `git clone https://gitlab.com/exploit-database/exploitdb.git` or CSV archive |
| **Format** | Plain text exploit files + CSV metadata index |
| **Size** | ~300 MB (full repo) |
| **Update cadence** | Daily — new exploits added continuously |
| **Incremental?** | Yes — `git pull`. New files appear in `exploitdb/` directory. |
| **Grimoire parser** | Not yet implemented — prose chunker would handle it, but value per chunk is low |

**Recommended ingest schedule:** Weekly if used. High noise ratio — lots of PoC code that's not useful as knowledge base content unless you're doing exploitability validation.

---

### MITRE ATT&CK — ICS Domain

| Field | Value |
|-------|-------|
| **Download** | `https://raw.githubusercontent.com/mitre/cti/master/ics-attack/ics-attack.json` |
| **Format** | STIX 2.1 JSON |
| **Size** | ~5 MB |
| **Update cadence** | Monthly (same release cycle as Enterprise) |

**Recommended ingest schedule:** Monthly, alongside Enterprise ATT&CK. Same parser applies.

---

### MITRE ATT&CK — Mobile Domain

| Field | Value |
|-------|-------|
| **Download** | `https://raw.githubusercontent.com/mitre/cti/master/mobile-attack/mobile-attack.json` |
| **Format** | STIX 2.1 JSON |
| **Size** | ~10 MB |
| **Update cadence** | Monthly (same release cycle as Enterprise) |

**Recommended ingest schedule:** Monthly, alongside Enterprise ATT&CK. Same parser applies.

---

### CIS Benchmarks

| Field | Value |
|-------|-------|
| **URL** | https://www.cisecurity.org/cis-benchmarks |
| **Format** | PDF (free members get XLSX summaries) |
| **Size** | Individual PDFs 2-10 MB each |
| **Update cadence** | ~Quarterly per benchmark |
| **Incremental?** | No — full PDF replacement |
| **Grimoire parser** | Not yet implemented — needs PDF extraction (existing `grimoire` PDF chunker may work) |

**Recommended ingest schedule:** On-demand per benchmark. Requires CIS membership for automated access.

---

## Recommended Ingest Schedule Summary

| Source | Frequency | Method | Incremental? |
|--------|-----------|--------|-------------|
| MITRE ATT&CK (all domains) | Monthly | `curl` from GitHub, check tags | No (full replace) |
| NVD CVE | Daily | API `lastModStartDate` | Yes |
| CISA KEV | Daily | `curl` full JSON (tiny) | No (full replace, ~500 KB) |
| Sigma rules | Weekly | `git pull` | Yes (git diff) |
| MITRE CAPEC | Quarterly | `curl` from GitHub | No (full replace) |
| OWASP Cheat Sheets | Monthly | `git pull` | Yes (git diff) |
| MITRE D3FEND | Quarterly | `curl` from d3fend.mitre.org | No (full replace) |
| NIST 800-53 | On-demand | Manual download | No |
| Exploit-DB | Weekly (if used) | `git pull` | Yes (git diff) |
| CIS Benchmarks | On-demand | Manual (requires membership) | No |

---

## Implementation Priority for Grimoire

1. **CISA KEV** — tiny file, easy JSON, massive utility (turns CVEs into actionable "patch now" signals)
2. **Sigma full repo** — parser already exists, just needs `git pull` + re-ingest workflow
3. **ATT&CK ICS + Mobile** — same parser as Enterprise, zero new code needed
4. **CAPEC** — STIX format, parser close to ATT&CK's
5. **D3FEND** — needs new ontology adapter, but maps offense → defense
6. **OWASP** — standard Markdown, trivial with existing chunker
7. **Exploit-DB** — large, noisy, low signal-per-chunk ratio
8. **NIST 800-53** — rarely changes, manual download is fine
9. **CIS Benchmarks** — requires membership for automated access