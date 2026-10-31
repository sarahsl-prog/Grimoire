# AI Review Verification — Audit Fix Commits

**Date:** 2026-10-31
**Subject:** Automated `ai-review` comments on PR #114 (`audit-updates` branch)
**Scope:** The 7 per-commit AI reviews covering audit phases 0 through 4b
(`eaefc14`, `95e8b68`, `2cada41`, `a317aa2`, `1fc4a41`, `acfd017`, `a3e4366`).
The phase-5 commit (`5993a10`) never got an AI review; the Ollama review
timed out.

## Summary

The bot reviewed each commit's diff on its own, without the rest of the
codebase. So it was right about most of the important problems, but about
a quarter of its findings are false positives: things that are fine once
you read the surrounding code. It also missed the most concrete regression:
**the audit commits broke 4 existing unit tests.**

Every finding below was checked against the code at `6a6d9d6`.

---

## Status

| Item | Status |
|------|--------|
| C-2 ChromaDB auth | Done: token removed, network isolation documented (`docs/deploy/docker.md`). |
| V-1 public ports | Done: api/mcp/pgadmin/redis-commander bound to loopback; Caddy proxy added (profile `proxy`). |
| Security overlay port tests | Done: two deploy tests broken by phase 2 updated to the env-driven port design. |
| CORS (`["*"]` in env templates) | Done: default `[]` in code and templates, origins validated. |
| Redis outage → API-wide 500s (found while checking `test_security.py`) | Done: slowapi in-memory fallback enabled. |
| Everything else | Open; see the follow-up backlog below. |

## Follow-up backlog

Open work, in recommended order. Tick items off here as they land (per
CLAUDE.md, one fix per commit). IDs refer to the sections below; items
marked *new* were found after the original review.

### Before the first VPS deploy (manual steps)

- [ ] Copy `.env` fresh from `.env.example`, or edit an existing one: set
      `GRIMOIRE_AUTH__CORS_ORIGINS=[]` (an old `.env` still has `["*"]`,
      which overrides the new default).
- [ ] Set `GRIMOIRE_DOMAIN` and `ACME_EMAIL`; point DNS at the server.
- [ ] Hetzner Cloud Firewall: inbound 22 (your IP), 80/tcp, 443/tcp,
      443/udp only.
- [ ] Validate the Caddyfile (never run against a real Caddy binary yet):
      `docker compose --profile proxy run --rm caddy caddy validate --config /etc/caddy/Caddyfile`
- [ ] After `up`, from outside the VPS: `nmap -Pn -p 1-10000 <vps-ip>`
      shows only 22, 80, 443; `curl https://<domain>/health` returns ok.

### Code

1. [x] **M-1: fix the 4 unit tests the audit commits broke**
       (`test_query_agent.py` `_cache_key` calls missing `top_k`;
       `test_watcher_agent.py::test_should_not_process_deleted`).
2. [x] *New:* **CLI cache stats crash.** Fixed: both callers now await it. Phase 5 made
       `DiskCache.get_stats()` async; `grimoire/cli/status.py` still calls it
       synchronously (`'coroutine' object has no attribute 'get'`). 4 failing
       tests: `test_cache.py::TestDiskCacheClose` (2) and
       `test_cli.py` cache-stats tests (2).
3. [x] *New:* **Per-tier rate limits are never applied.** Done: two-stage limiter (per-IP pre-auth, per-key tier after auth) plus a per-IP invalid-key cap, on REST and MCP; the MCP service previously had no limiter at all. *Worse than first described:* `SlowAPIMiddleware` checks limits before routing, so `request.state.api_key` is never set and **every request is limited by client IP at 30/minute**, whatever its key. Needs a design decision (pre-auth vs post-auth limiting); see PR notes.
       `DEFAULT_TIER_RATE_LIMITS` (`grimoire/api/auth.py`),
       `AuthConfig.rate_limits` / `GRIMOIRE_AUTH__RATE_LIMITS`, and
       `get_tier_rate_limit()` (`grimoire/api/rate_limit.py`) exist but
       nothing uses them. Every key gets the 30/minute default, so agent keys
       (documented 600/min) get throttled early. Check whether the key's tier
       is known when `SlowAPIMiddleware` evaluates limits (it runs before
       route dependencies). Keep `in_memory_fallback_enabled=True`.
4. [x] **C-3, C-4, C-5: shared vector-store factory** Done, plus: search hides orphaned vectors, `grimoire reindex` dimension bug fixed. used by
       `documents.py`, `mcp/tools.py`, `watcher.py`, `api/main.py`,
       `cli/status.py`, with one delete contract (commit DB first, then
       delete vectors; log orphaned IDs on failure).
5. [x] **C-1, M-2: read-only Postgres role for `pg_query`** Done: migration 0008, `grimoire db readonly-role` run by db-migrate, pg_query refuses until `POSTGRES_READONLY_PASSWORD` is set. Verified against a real PostgreSQL 16 (`tests/integration/test_readonly_role.py`). Was: (see effort
       notes). Closes the `query_to_xml` bypass. *Partly done:* the LIMIT
       wrapper is gone; the cap is now `fetchmany(limit)` on a server-side
       cursor and `;` is rejected. The role is still needed.
6. [x] **C-6, C-10: watcher.** Done, plus MOVED events (renames and atomic saves) and processor restart. Extension filter on DELETED events; check
       the file is still gone before deleting (save-via-rename race); clear
       `_event_loop` on stop.
7. [x] **C-7: cache invalidation.** Done. Correction to the original
       finding: invalidation *does* reach the API from the CLI watcher,
       because both backends are shared stores. Also fixed: the Redis cache
       backend crashed on construction, and every invalidation wiped cached
       embeddings and generated content. Not covered: `CoordinatorAgent`
       ingests through a caller-owned session, so its caller must invalidate
       after committing.
       Original text: Drop the `hasattr` guards, invalidate on
       REST ingest, document that the CLI watcher can't clear the API's
       cache.
8. [x] **C-8, C-9: observability and startup.** Done (OTLP provider, Redis instrumentation, watcher status in `/readyz`; `/readyz` also stopped leaking error details). Replace `except: pass` in the
       OTel blocks with warnings; configure a `TracerProvider` + OTLP
       exporter or drop the OTel deps; surface a failed `WatcherAgent` start
       in `/readyz`.
9. [x] **C-12: test gaps** Done: pagination bounds, ChromaDB error mapping across `to_thread`, `/readyz` (had no tests). not covered by the items above (pagination
       bounds, `to_thread` exception mapping, a non-mocked delete
       integration test).
10. [x] **Compose `${VAR:?}` guards** Decided: start-time check instead (Redis exits with a clear message; Postgres already does). Was: for `POSTGRES_PASSWORD` /
        `REDIS_PASSWORD`.** Note: Compose interpolates every service, so a
        guard makes all `docker compose` commands fail when the variable is
        unset, including in CI and the deploy tests. Decide whether that's
        wanted, or validate at container start instead (as the caddy
        service does).
11. [x] **C-11:** Done: `examples/` deleted. delete or clearly quarantine `examples/rag_pipeline.py`
        (pickle deserialization via `allow_dangerous_deserialization=True`).
12. [x] *New, low:* **stale Celery config.** Removed from `.env.example`, `grimoire.yaml.example` and the deploy docs. Celery was removed in phase 3,
        but `.env.example` still has a `GRIMOIRE_CELERY__*` block and
        `docs/deploy/docker.md` still lists Redis as the Celery broker. The
        variables are ignored at load time, so this is cleanup, not a bug.

### Also fixed on `audit-followups` (found while working the backlog)

- `grimoire reindex` could never write (opened the store with `embedding_dim=1`).
- The Redis cache backend crashed every agent builder (`RedisCache` got `path=`).
- Query-cache invalidation wiped all cached embeddings and generated content.
- Search could return chunks of deleted documents via orphaned vectors.
- Watcher ignored MOVED events (renames, atomic saves) and never restarted a
  dead processor.
- `/readyz` echoed raw dependency errors publicly.
- `bcrypt.checkpw` blocked the event loop on every authenticated request.
- Delete-vectors integration test used a non-UUID id (always 404).

## Missed by the AI review

### M-1. Four unit tests broken by the audit commits

```
FAILED tests/test_query_agent.py::TestQueryCaching::test_cache_key_deterministic
FAILED tests/test_query_agent.py::TestQueryCaching::test_cache_key_different_filters
FAILED tests/test_query_agent.py::TestQueryCaching::test_cache_key_case_insensitive
FAILED tests/test_watcher_agent.py::TestWatcherFiltering::test_should_not_process_deleted
4 failed, 164 passed   (test_query_agent, test_mcp, test_watcher_agent, test_api)
```

- Phase 4a added a required `top_k` argument to `QueryAgent._cache_key()`.
  The tests still call it with two arguments.
- Phase 1 changed `_should_process()` to accept DELETED events. The old test
  that asserts the opposite was never updated.

### M-2. `pg_query` restricted-table guard is bypassable

The `api_keys` blocklist is a regex over the SQL text. Postgres can build a
query from strings at execution time, so the table name never shows up in
the text:

```sql
SELECT query_to_xml('select * from api_' || 'keys', true, true, '')
```

This passes `PgQueryInput` validation and the table guard, and the
read-only transaction does not stop reads. Fixing this needs the
database-role fix (C-1 below), not a better regex.

---

## Confirmed findings

| ID | Severity | Finding | Location |
|----|----------|---------|----------|
| C-1 | High | `pg_query` wrapper escape: `SELECT * FROM documents) AS x --` closes the subquery and comments out `LIMIT :row_limit`, so the whole result loads into memory. The keyword regex also rejects legitimate queries (`ILIKE '%update%'`), and `set_config('statement_timeout','0',true)` can switch the timeout off inside the transaction. | `grimoire/mcp/tools.py:376`, `:1168` |
| C-2 | High | ChromaDB auth is half-wired. The server is given `CHROMA_SERVER_AUTHN_TOKEN`, but no client config sends a token. Chroma 1.x no longer has built-in server auth (see effort notes below), so the variable is most likely ignored and the "hardening" does nothing. `${REDIS_PASSWORD}` and `${CHROMA_SERVER_AUTHN_TOKEN}` have no `:?` guard. | `docker-compose.yml:97,110`, `docker-compose.security.yml:47` |
| C-3 | Medium | MCP delete builds and starts up the vector store outside the `try`, after the DB commit. A ChromaDB failure escapes as a raw exception instead of `_err(...)`. | `grimoire/mcp/tools.py:1110-1122` |
| C-4 | Medium | REST delete has no `try` around vector cleanup, even though the comment says failures are "logged and surfaced". It also deletes vectors *before* the DB row, which is the opposite order from MCP. Two contracts for one operation. | `grimoire/api/routes/documents.py:342-363` |
| C-5 | Medium | The same ChromaDB setup block (with the `embedding_dim=1` workaround) is copy-pasted in five places and rebuilt on every request. | `documents.py`, `mcp/tools.py`, `agents/watcher.py`, `api/main.py`, `cli/status.py` |
| C-6 | Medium | Watcher DELETED handling: (a) any file extension is accepted (`Path.suffix` works on a deleted path); (b) an editor's save-via-rename (DELETED then CREATED) can delete the freshly re-ingested document; (c) a failed vector delete is dropped with no retry. | `grimoire/agents/watcher.py:325`, `:425-460` |
| C-7 | Medium | Cache invalidation: it only works inside the API process, so a CLI `grimoire watch start` never clears the API's cache. The REST ingest route never invalidates. The `hasattr` guards turn wiring bugs into silent no-ops. | `watcher.py:509`, `documents.py:381` |
| C-8 | Low | OTel does nothing: no `TracerProvider` or exporter is configured anywhere; both setup blocks use `except Exception: pass`; `opentelemetry-instrumentation-redis` is installed but never called. | `grimoire/api/main.py:206`, `grimoire/db/session.py:52`, `pyproject.toml:45` |
| C-9 | Low | If the `WatcherAgent` fails to start in the API lifespan, it only logs a warning, and `/watch` routes are served with no watcher behind them. | `grimoire/api/main.py:120-137` |
| C-10 | Low | Watcher fallback path calls `asyncio.Queue.put_nowait` from the watchdog thread. `_event_loop` is never cleared on stop, and `call_soon_threadsafe` is not guarded against a closed loop. | `grimoire/agents/watcher.py:297-310` |
| C-11 | Low | `examples/rag_pipeline.py` still uses `allow_dangerous_deserialization=True` (pickle). The commit message says "remove", but the file was relocated. | `examples/rag_pipeline.py:48,63` |
| C-12 | Medium | Almost none of the new behaviour has tests: watcher DELETED handling, the READ ONLY / timeout settings in `pg_query`, cache invalidation, pagination limits, and the `to_thread` exception mapping. The "integration" delete test mocks the whole DB session. | `tests/` |

---

## False positives

| AI claim | Why it doesn't hold |
|----------|---------------------|
| `embedding_dim=1` breaks every delete or creates a 1-dim collection | ChromaDB collections take their dimension from the first insert. `ChromaDBStore.initialize()` only stores the number for add/query checks. Deletes work. It's a code smell (C-5), not a bug. |
| Hardcoding ChromaDB dropped the Qdrant abstraction | There is no Qdrant store implementation. The removed `grimoire.services.vector_store` import pointed at a module that doesn't exist, and that was the original C-1 audit bug. |
| Lazy `doc.chunks` raises `MissingGreenlet` / skips the cascade | `Document.chunks` is `lazy="selectin"` with `cascade="all, delete-orphan"`. |
| `logger` / `Query` / `get_settings` missing, causing `NameError` | All are imported at module level. |
| Cache invalidation after DELETE is unreachable | The endpoint is `status_code=204` and returns `None`, so the block runs. |
| Watcher CREATED events don't invalidate the cache | CREATED and MODIFIED share one branch that invalidates on `completed`. |
| Cache-key attributes may not exist | `_llm_model`, `_temperature`, `_max_context_chunks` are set in `__init__`. |
| CI: unpinned Trivy / missing `permissions` / `uv` "latest" | The current `ci.yml` pins every action by SHA, sets `permissions`, and pins `UV_VERSION`. |
| Phase 0 skipped `old.env.bak` (F-10) | The file isn't in the tree or the history, and `*.bak` is in `.gitignore`. |
| Lingering Celery imports | None remain in `grimoire/`. |

---

## Recommended fix order

One fix per commit, each with tests (per CLAUDE.md):

1. **M-1:** update the 4 broken tests so CI is green again.
2. **C-3, C-4, C-5:** add one `get_vector_store(settings)` factory that is
   started once and reused, and use it in all five call sites. One delete
   contract: commit to the DB first, then delete vectors; on failure, log the
   document ID and orphaned vector IDs at ERROR level and return a clean error.
3. **C-1, M-2:** read-only database role for `pg_query` (see below), plus
   `fetchmany(limit)` as a row cap that doesn't depend on the SQL text.
4. **C-2:** pick a ChromaDB access-control approach (see below) and remove
   the misleading env var. Add `${VAR:?}` guards.
5. **C-6, C-10:** watcher: extension filter on DELETED, check the file is
   still gone before deleting, clear `_event_loop` on stop.
6. **C-7:** remove the `hasattr` guards, invalidate on REST ingest, and
   document the cross-process limitation.
7. **C-8, C-9:** log instead of `pass`; either configure a `TracerProvider`
   with an OTLP exporter or remove the OTel dependencies; make watcher
   startup failure show up in `/readyz`.

---

## Effort notes

### C-1: dedicated read-only Postgres role for `pg_query`

**Effort: moderate.** About half a day: roughly 6 files and 150–250 lines
including tests.

This moves enforcement from regexes into the database, which is the only
layer that can't be talked around (it also closes M-2 and the
`set_config` timeout trick, because the role can carry its own fixed
timeout).

| Piece | Work |
|-------|------|
| Role creation | Alembic migration that creates `grimoire_readonly` as `NOLOGIN`, grants `USAGE` on the schema and `SELECT` on all tables, `REVOKE ALL ON api_keys`, and `ALTER DEFAULT PRIVILEGES` so future tables are covered (with `api_keys` re-revoked in any migration that creates sensitive tables). Also `ALTER ROLE grimoire_readonly SET statement_timeout = '30s'`. |
| Password | Keep the secret out of the migration. A small `scripts/db_set_readonly_password.sh` (or a startup hook) runs `ALTER ROLE ... LOGIN PASSWORD` from `POSTGRES_READONLY_PASSWORD` in `.env`. Note that `docker-entrypoint-initdb.d` only runs on a fresh volume, so it can't be the only path for existing installs. |
| Settings | New `database.readonly_url` (optional) in `grimoire/config/settings.py`, `.env.example`, `grimoire.yaml.example`, and the compose `x-grimoire-env` block. |
| Engine | A second small, lazily created engine (pool size 1–2) in `grimoire/db/session.py`, used only by `grimoire_pg_query`. |
| Fallback | If `readonly_url` is unset or the DB is SQLite (dev), either disable `pg_query` with a clear error (recommended) or keep today's regex path with a logged warning. |
| Tests | Unit tests for engine selection and fallback; an integration test (existing `tests/integration` setup) proving that `INSERT`, `SELECT FROM api_keys`, and the `query_to_xml` bypass all fail with a permission error. |

After this, the keyword regex and table blocklist can stay as cheap early
rejections with friendlier errors, but they are no longer the security
boundary.

### C-2: ChromaDB client-token auth

**Effort: small on the client, but the server side is the catch.**

*Client side (easy, about 1 hour).* `chromadb.HttpClient` accepts a
`headers=` argument. Add an optional `vector_store.auth_token` setting
(`SecretStr`), pass `headers={"Authorization": f"Bearer {token}"}` when
it's set, and wire `GRIMOIRE_VECTOR_STORE__AUTH_TOKEN` into compose. Once
the C-5 factory exists, that's a change in one place. Without the factory,
it's 12 constructor call sites.

*Server side (the real cost).* Chroma 1.0 rewrote the server in Rust and
**dropped built-in authentication** ([migration guide][chroma-migration]).
The repo pins the `chromadb/chroma:1.5.9` image, so
`CHROMA_SERVER_AUTHN_TOKEN` is very likely ignored, and nothing would check
a client token. Confirm with `curl` against `/api/v2/heartbeat` with no
header; a 200 means auth is off. The options:

| Option | Effort | Notes |
|--------|--------|-------|
| A. Network isolation only (recommended for a personal deployment) | ~30 min | Remove the ChromaDB host port mapping, or keep it on `127.0.0.1` for debugging, so only the internal `grimoire-network` can reach it. Delete the misleading `CHROMA_SERVER_AUTHN_TOKEN` lines and document the decision. |
| B. Token-checking reverse proxy | ~half a day | Small Caddy or nginx sidecar in front of ChromaDB that rejects requests without the right `Authorization: Bearer` header, plus the client-side change above. Adds a container and a health check. |
| C. Downgrade to a pre-1.0 image with built-in token auth | Not recommended | Gives up the 1.x fixes and conflicts with the locked `chromadb` 1.5.5 client. |

#### Decision for the Hetzner VPS deployment

**Use Option A (network isolation), tightened for a public host.** Nothing
off the VPS needs to reach ChromaDB: the `api`, `mcp` and `watcher`
containers talk to it over `grimoire-network`. A token proxy would only
protect a port that shouldn't be reachable in the first place.

One VPS-specific trap: **Docker's published ports bypass UFW/iptables
rules.** Docker writes its own NAT rules ahead of UFW's, so `ufw deny 8060`
does nothing for a container port. The only reliable controls are how a
port is published and the Hetzner Cloud Firewall, which sits outside the
VM.

Steps:

1. Remove the `ports:` mapping from `chromadb` in `docker-compose.yml`.
   Loopback binding (`127.0.0.1:8060`) is safe on a single-user VPS, but
   with no mapping there's nothing to misconfigure later.
2. Delete `CHROMA_SERVER_AUTHN_TOKEN` from both compose files and
   `.env.example`, and add a comment explaining that ChromaDB 1.x has no
   built-in auth and is reachable only on the internal network.
3. For debugging from a laptop, use an SSH tunnel to the container IP
   instead of publishing a port:
   `ssh -L 8060:<chromadb-container-ip>:8000 user@vps`. Get the IP with
   `docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' grimoire-chromadb`.
   A fixed IP can be pinned in `grimoire-network` (subnet `172.28.0.0/16`)
   if you do this often.
4. Hetzner Cloud Firewall: inbound allow only 22 (ideally limited to your
   IP) and 80/443. Default-deny everything else. This is the backstop if a
   compose change ever publishes a port by accident.
5. Verify from outside the VPS: `nmap -Pn -p 1-10000 <vps-ip>` should show
   only 22, 80 and 443.

Revisit Option B only if ChromaDB ever has to be reached from another
machine (a second VPS or a hosted worker). Even then, a private network
(Hetzner vSwitch/private network or Tailscale) is simpler than a token
proxy.

#### Related VPS exposure found while checking this (V-1, High)

These services publish on **all interfaces** (`0.0.0.0`), so on a VPS
they are reachable from the internet:

| Service | Port | Risk |
|---------|------|------|
| `api` | 8001 | Plain HTTP, so API keys travel unencrypted. |
| `mcp` | 8100 | Same. |
| `pgadmin` (`tools` profile) | 5050 | Defaults to `admin@grimoire.local` / `admin`. Full database admin. |
| `redis-commander` (`tools` profile) | 8081 | No HTTP auth configured. |

Fix: bind `api` and `mcp` to `127.0.0.1` and put a TLS reverse proxy
(Caddy is the least work: automatic Let's Encrypt) in front of them on
443. Never run the `tools` profile on the VPS. If you need pgAdmin, bind
it to `127.0.0.1` and use an SSH tunnel. Remove the `:-admin` default
password so it fails closed. Effort: about 2–3 hours including a Caddy
service and `Caddyfile`.

Also watch [CVE-2026-8828][cve] (authorization bypass across tenants in the
1.x Rust server) if multi-tenant ChromaDB is ever used.

[chroma-migration]: https://docs.trychroma.com/docs/overview/migration
[cve]: https://nvd.nist.gov/vuln/detail/CVE-2026-8828
