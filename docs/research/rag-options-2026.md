# RAG Enhancement Options & OKF Evaluation

_Research report, 2026-10-30. Scope: what to add to Grimoire's retrieval pipeline next, and whether Google's Open Knowledge Format (OKF) is useful for the security corpus._

---

## 1. Where Grimoire is today

| Stage | Current implementation | File |
|---|---|---|
| Chunking | Source-type dispatch (Sigma / NVD / MITRE / playbook / prose) + recursive, markdown and semantic chunkers | `grimoire/strategies/security/chunker.py`, `grimoire/core/chunker/` |
| Dense retrieval | ChromaDB, single-vector embeddings via Ollama / sentence-transformers | `grimoire/search/vector.py`, `grimoire/core/embedder.py` |
| Lexical retrieval | PostgreSQL FTS | `grimoire/search/fulltext.py` |
| Fusion | Weighted scores, then **raw addition** when a chunk appears in both lists | `grimoire/search/hybrid.py:322` (`_merge_results`) |
| Rerank | Cross-encoder (`CrossEncoderReranker`) | `grimoire/core/reranker.py` |
| Security rerank | Regex intent classification → severity × recency × intent/source multipliers | `grimoire/strategies/security/retriever.py` |
| Generation | Single-shot Ollama `/api/generate`, top-N chunks concatenated as `[Source N]` | `grimoire/agents/query.py` |

### Two issues found while reviewing (fix before adding features)

1. **MITRE ATT&CK techniques are scored to zero.** `parsers/mitre.py` sets every technique to `Severity.UNKNOWN`, and the default `severity_weights["unknown"]` is `0.0` (`config/settings.py:1016`). `_security_rerank` multiplies by that weight, so every ATT&CK chunk ends with score 0, including for `technique_lookup` queries where the intent matrix is supposed to *boost* them. Same for any prose/playbook chunk without severity. The tests (`tests/strategies/test_security_retriever.py:300`) currently assert this behaviour. Suggested fix: make severity a boost only for source types that *have* severity (CVE, Sigma), or default `unknown` to `1.0`.
2. **Score fusion adds incompatible scales.** Cosine similarity and `ts_rank` are on different, unnormalised scales, and overlapping hits are summed. Ranking depends on the score distributions more than on relevance. Reciprocal Rank Fusion (RRF) fixes this (see option A1).

---

## 2. RAG options

Ratings are relative to **this** codebase: local Ollama, PostgreSQL + ChromaDB, 100K+ docs, a security corpus full of exact identifiers (CVE-IDs, T-IDs, IOCs).

**Effort:** S = days, M = 1–2 weeks, L = multi-week. **Impact:** expected retrieval/answer-quality gain for Grimoire's workload.

### Tier A: cheap wins on the current pipeline

#### A1. Reciprocal Rank Fusion (RRF) — Effort S, Impact High
Replace weighted score addition with `score = Σ 1/(k + rank_i)` (k≈60).
- **Pros:** Scale-free, so you no longer need to tune `vector_weight`/`fts_weight`. Standard practice in Elastic, OpenSearch, Weaviate and pgvector recipes. Roughly 30 lines in `_merge_results`.
- **Cons:** Throws away the score magnitude (a near-perfect FTS hit counts the same as a merely top-ranked one). Downstream code that treats `score` as a 0–1 confidence (security multipliers, citation `relevance_score`) needs re-checking.

#### A2. Exact-identifier routing — Effort S, Impact High (security)
`_classify_query` already detects CVE / technique / IOC intent. Use that to run a direct SQL lookup on `cve_id` / `mitre_technique_id` first and pin those hits above the fused list.
- **Pros:** Embeddings are bad at identifiers (`CVE-2024-3094` vs `CVE-2024-3049` look nearly identical to them). Deterministic, cheap and easy to explain. The metadata fields already exist and are validated.
- **Cons:** Only helps identifier-bearing queries. Needs an index on the ID columns.

#### A3. Contextual chunk headers ("contextual retrieval") — Effort S–M, Impact High
Prepend document-level context to each chunk before embedding and FTS indexing. Anthropic reported large drops in retrieval failure (≈49% with contextual embeddings + BM25, ≈67% with reranking added; vendor-reported).
- **Grimoire twist:** For structured sources you don't need an LLM. Build the header deterministically, e.g. `CVE-2024-3094 · xz-utils · CWE-506 · CVSS 10.0 ·` or `T1059.001 PowerShell · Execution ·`. Use the LLM only for prose/PDF chunks.
- **Pros:** Large gain for small code. Fits the existing chunker dispatch. Deterministic headers cost nothing at ingest.
- **Cons:** Needs a full re-embed and re-index. LLM-generated headers for 100K+ prose docs are slow on local Ollama, so batch and cache them. Slightly larger chunks.

#### A4. Upgrade the reranker — Effort S, Impact Medium–High
Swap or benchmark the cross-encoder against current open models (e.g. BGE-reranker-v2-m3, mxbai-rerank-v2, Qwen3-Reranker family).
- **Pros:** Drop-in behind the existing `Reranker` ABC. Usually the biggest single quality lever after fusion.
- **Cons:** Bigger models add latency on CPU, so you may want `docker-compose.gpu.yml`. Pick the model with an eval set, not leaderboard vibes (see A6).

#### A5. Query transformation (multi-query / HyDE / step-back) — Effort S, Impact Low–Medium
- **Pros:** Helps vague natural-language questions ("how do attackers persist on Linux?").
- **Cons:** HyDE asks the LLM to *invent* a hypothetical answer, which is a hallucination vector exactly where you can't afford one (fake CVE IDs). Extra LLM call per query. **Recommendation:** use multi-query only for the `general_security` intent; never for ID lookups.

#### A6. Evaluation harness — Effort S–M, Impact enables everything else
A golden set of ~100–200 question → expected-chunk pairs from your real security queries, plus metrics (recall@k, MRR, nDCG) and answer faithfulness (RAGAS-style or an LLM-judge). Log runs to MLflow, which matches the CLAUDE.md traceability standard.
- **Pros:** Without this, every option below is guesswork. Turns "feels better" into numbers.
- **Cons:** Building the golden set is manual work. LLM-judge metrics are noisy on small local models.

### Tier B: structural upgrades

#### B1. Parent-document / small-to-big retrieval — Effort M, Impact Medium
Retrieve on small chunks but feed the parent section (or whole CVE/Sigma rule) to the LLM.
- **Pros:** Structured security docs are short, so returning the whole record is often ideal. Fixes answers that cut off mid-rule.
- **Cons:** Larger context, which matters with an 8K default Ollama context. Needs a parent-ID mapping, which you mostly have via `document_id` + `chunk_index`.

#### B2. Late-interaction retrieval (ColBERT family) — Effort L, Impact Medium–High
Token-level multi-vector matching (ColBERTv2, Jina-ColBERT, via PyLate).
- **Pros:** Much better on jargon-heavy and rare-term text than single-vector embeddings. More interpretable (you can see which tokens matched).
- **Cons:** **ChromaDB has no native multi-vector/MaxSim support**, so this means a vector-store change (Qdrant, LanceDB, Vespa, or pgvector + PLAID-style index). Storage is 10–50× single-vector. A big lift for a gain that A3 + A4 may mostly capture. **Defer until the eval set shows a gap.**

#### B3. Learned sparse retrieval (SPLADE / BGE-M3 sparse) — Effort M, Impact Low–Medium
- **Pros:** Gives FTS semantic expansion.
- **Cons:** WordPiece tokenisation fragments identifiers (`CVE`, `-`, `2024`…), which is a regression vs Postgres FTS for this corpus. Another index to operate. **Not recommended** as an FTS replacement; at most as an extra leg in RRF.

#### B4. Corrective / self-checking RAG (CRAG, Self-RAG-style grading) — Effort M, Impact Medium–High
After retrieval, a light grader decides whether the context is sufficient. If not, re-query, broaden filters, or return "insufficient evidence". Optionally post-check that every cited `[Source N]` supports its claim.
- **Pros:** Directly reduces confident wrong answers, which is the biggest risk for a security team. Fits the "never surface raw errors" standard. Can reuse the reranker score as the grader (no extra LLM call).
- **Cons:** Adds latency. Threshold tuning needs A6.

#### B5. Agentic RAG (retrieval-as-tools) — Effort M–L, Impact High for multi-step questions
Expose `search`, `get_cve`, `get_technique`, `get_related(id)`, `filter_by_date` as tools to the Deep Agents loop already chosen in `DESIGN.md`, and let the model iterate.
- **Pros:** Handles "which of our Sigma rules detect techniques used against CVE-X?" style multi-hop questions. Reuses the MCP server surface, and Claude/Cursor clients already work this way.
- **Cons:** 3–10× the LLM calls. Small local models loop or stop early. Harder to evaluate and trace (OTel/MLflow spans become important). **Best paired with routing:** agentic only for complex intents, single-shot for lookups.

### Tier C: graph-based RAG

Security data is *natively a graph*: CVE → CWE → CAPEC → ATT&CK technique → Sigma rule → IR playbook, plus product/vendor edges. That makes this tier more relevant to Grimoire than to a generic knowledge base.

#### C1. Deterministic security knowledge graph — Effort M, Impact High (recommended)
Build edges **from the structured fields you already parse** (NVD CWE refs, STIX relationships, Sigma `tags: attack.t1059`, playbook front-matter) into a Postgres `entity_edges` table. At query time, expand the top hits 1–2 hops and add neighbours as candidates into RRF.
- **Pros:** No LLM extraction, so it's cheap, reproducible and has no hallucinated edges. Uses existing Postgres (recursive CTEs are enough at this scale). Fills in the `get_related_documents(doc_id)` stub in `DESIGN.md`. Becomes the `get_related` tool for B5.
- **Cons:** Only covers relations present in structured sources; prose PDFs get no edges. Hop expansion can flood results without a per-hop decay.

#### C2. LLM-extracted GraphRAG (Microsoft GraphRAG, LightRAG, HippoRAG 2, LazyGraphRAG)
| | MS GraphRAG | LightRAG | LazyGraphRAG | HippoRAG 2 |
|---|---|---|---|---|
| Indexing cost | Very high (LLM extraction + community summaries) | High (LLM extraction) | ≈ vector RAG (Microsoft: 0.1% of GraphRAG) | Medium (OpenIE extraction) |
| Strength | Global "summarise the corpus" questions | Incremental updates, local+global modes | Cheap; good global answers | Multi-hop / associative recall |
| Local Ollama fit | Poor at 100K docs | OK with ≥32K context models | Good | OK |
- **Pros:** Covers relationships hidden in unstructured text (reports, PDFs, blog-style intel).
- **Cons:** GraphRAG-Bench (reported at ICLR 2026) found graph methods *tie* plain chunks on simple fact retrieval and win only on complex reasoning. LLM extraction on a local model creates noisy entities and invented edges. Re-indexing cost on every corpus refresh. **Recommendation:** do C1 first. Revisit LightRAG or LazyGraphRAG for the prose/PDF subset only if the eval set shows multi-hop failures C1 can't fix.

#### C3. Visual document retrieval (ColPali/ColQwen) — Effort L, Impact Low
- **Pros:** Retrieves from page images, so tables and diagrams survive.
- **Cons:** Docling already extracts tables well. Multi-vector storage again. Not where security queries fail. **Skip for now.**

---

## 3. OKF (Open Knowledge Format)

### What it is
- Google Cloud open spec (Apache-2.0, `GoogleCloudPlatform/knowledge-catalog`, `okf/SPEC.md`). v0.1 was released June 2026, and v0.2 (July 24, 2026) added trust signals.
- A **bundle** is a folder of UTF-8 Markdown files. Each file is a *concept*, its path is the concept ID, and YAML frontmatter requires only `type` (`title` recommended; extension keys allowed).
- Concepts link with plain Markdown links, treated as **untyped directed edges**. Consumers must tolerate unknown types, missing fields and broken links.
- v0.2 adds optional provenance/trust fields: `generated: {by, at}`, `verified: [{by, at}]` (any `human:<id>` gives the "human-reviewed" tier), `sources: [...]` with footnote citations, `status: draft|stable|deprecated`, and `stale_after: <date>`. It also adds an `Attested Computation` type for sanctioned, verifiable queries (BigQuery-centric examples).

### Fit with Grimoire's security data

| Data | OKF fit | Why |
|---|---|---|
| **IR playbooks** | **Good** | Already Markdown + YAML frontmatter (`parsers/playbook.py` reads `severity` from front matter). OKF is close to a superset of the current format. `verified` / `status` / `stale_after` model playbook review cycles well. |
| Internal detection notes, threat models, runbooks, analyst write-ups | **Good** | Team-authored knowledge, which is OKF's target use case. Trust tier separates reviewed from draft. |
| Sigma rules | Partial | Sigma has its own `status` (experimental/test/stable) and must stay valid Sigma YAML for tooling. OKF could at most be a *wrapper/export view*. |
| CVE (NVD JSON 2.0), ATT&CK / CAPEC (STIX 2.1), IOCs | **Poor** | These already have authoritative, typed, machine-readable formats. OKF's untyped links would *lose* STIX relationship types (`uses`, `mitigates`, `detects`). 300K+ CVEs as Markdown files adds nothing. OKF is **not** a threat-intel exchange standard; STIX/TAXII and MISP are. |

### Pros
- Trust/provenance fields line up with RAG retrieval signals Grimoire already computes. `stale_after` and `generated.at` plug straight into the recency multiplier, `status: deprecated` becomes a filter, and the `human-reviewed` tier can be a boost. That is a principled replacement for some hand-tuned multipliers.
- Gives playbooks and analyst notes a portable, git-friendly format that agents (Claude/Cursor via MCP) and humans can both read.
- Export path: Grimoire could publish a curated **OKF bundle** (e.g. one concept per ATT&CK technique linking its CVEs, Sigma rules and playbooks). That's a static, offline-shareable complement to the MCP server.
- Low ingest cost for Markdown sources: parse frontmatter, which you already do.

### Cons
- **Very young (v0.x, ~4 months old).** Fields have already been renamed (`timestamp` → `generated.at`), so expect churn.
- Tooling is Google-centric (reference agent, BigQuery, Knowledge Catalog) and there is no mature Python library. You would write a small parser/validator yourself.
- Trust tiers are **advisory only**: no signatures, no access control, nothing verifies the claims.
- Untyped links are weaker than the typed graph you'd build in C1.
- Adoption outside Google's ecosystem is unproven; most coverage so far is secondary blogs.

### Recommendation on OKF
1. **Adopt the ideas now, not the format.** Add `generated_at`, `verified_by`, `status`, and `stale_after` to `SecurityMetadata` and use them in `_security_rerank`. This gives the value without the spec risk.
2. **Accept OKF frontmatter in the playbook parser** (cheap: tolerate and map `type`, `status`, `verified`, `stale_after`, `sources`). Playbooks authored in OKF then just work.
3. **Don't convert CVE / STIX / Sigma to OKF.** Keep native formats as the source of truth.
4. **Revisit OKF bundle export** after v1.0 or once a second major vendor adopts it.

---

## 4. Suggested roadmap

| # | Item | Effort | Depends on |
|---|---|---|---|
| 0 | Fix `unknown` severity zeroing ATT&CK/prose scores | S | — |
| 1 | Eval harness + golden set, logged to MLflow (A6) | S–M | — |
| 2 | RRF fusion (A1) | S | 1 (to measure) |
| 3 | Exact-ID routing (A2) | S | — |
| 4 | Deterministic contextual headers for structured sources (A3) | S–M | re-index |
| 5 | Reranker bake-off (A4) | S | 1 |
| 6 | Trust/staleness metadata + OKF-tolerant playbook parser (OKF §1–2) | S–M | — |
| 7 | Deterministic security knowledge graph + hop expansion (C1) | M | 2 |
| 8 | Corrective RAG grading / citation check (B4) | M | 1 |
| 9 | Agentic retrieval tools on Deep Agents + MCP (B5) | M–L | 7 |
| — | Defer: ColBERT (B2), LLM GraphRAG (C2), ColPali (C3), SPLADE (B3) | | eval evidence |

---

## Sources
- Google Cloud blog, "OKF v0.2 adds trust signals" — https://cloud.google.com/blog/products/data-analytics/okf-v0-2-adds-trust-signals
- OKF overviews (secondary): https://letsdatascience.com/news/google-publishes-open-knowledge-format-for-knowledge-bundles-9756381a · https://wavect.io/blog/open-knowledge-format-okf/ · https://www.iloveseo.net/the-open-knowledge-format-what-google-really-shipped · https://codex.danielvaughan.com/2026/06/18/okf-implementation-guide-building-agent-ready-knowledge-bundles-codex-cli-mcp/
- Microsoft Research, "LazyGraphRAG: Setting a new standard for quality and cost" — https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost
- Graph RAG survey — https://arxiv.org/pdf/2408.08921 · Awesome-GraphRAG — https://github.com/DEEP-PolyU/Awesome-GraphRAG
- A-RAG (hierarchical agentic retrieval) — https://arxiv.org/pdf/2602.03442
- RAG technique comparisons (secondary): https://blog.starmorph.com/blog/rag-techniques-compared-best-practices-guide · https://atlan.com/know/advanced-rag-techniques/ · https://www.turingpost.com/p/ragtypes
- STIX/TAXII background — https://www.cloudflare.com/learning/security/what-is-stix-and-taxii/

_Caveat: the primary OKF spec (`okf.md`, `SPEC.md`) could not be fetched from this environment, so v0.1 structure details come from Google's v0.2 post and secondary write-ups. Verify field names against `SPEC.md` before implementing. Performance figures (Anthropic contextual retrieval, LazyGraphRAG cost ratios, GraphRAG-Bench) are as reported by their authors, not re-measured._
