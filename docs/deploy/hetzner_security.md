# Hetzner deploy — Grimoire security mode

This is the one-shot homelab guide for standing up Grimoire's
security-domain pipeline on a Hetzner Cloud VM (or any Ubuntu / Debian
box, really — Hetzner is just the reference). Follows from the
[security strategy plan](../plans/security_strategy_plan.md); for the
filter / settings surface see
[`docs/strategies/configuration.md`](../strategies/configuration.md) and
[`docs/strategies/usage.md`](../strategies/usage.md).

## Sizing

| VM | vCPU | RAM | Disk | Notes |
|---|---|---|---|---|
| **CPX21** (recommended) | 3 | 4 GB | 80 GB NVMe | Comfortable for Sigma + MITRE + one year of NVD CVE. ChromaDB sits at ~1–2 GB resident. |
| **CPX31** | 4 | 8 GB | 160 GB NVMe | Pick this if you plan to ingest multiple NVD years or large prose corpora. |
| CPX11 | 2 | 2 GB | 40 GB | Will swap under load — only for kicking the tyres. |

The Phase 10 overlay deliberately does **not** ship a local Ollama
container; you point ``GRIMOIRE_LLM__URL`` at a remote endpoint (cloud
Ollama, OpenAI-compatible service, or a separate GPU box).

## One-shot bootstrap

```bash
# 1. Become a non-root user, install docker.
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git curl
sudo usermod -aG docker "$USER"
newgrp docker

# 2. Clone Grimoire and pin to a release tag.
git clone https://github.com/sarahsl-prog/Grimoire.git /srv/grimoire
cd /srv/grimoire

# 3. Configure secrets.
cp .env.security.example .env
# Edit .env: rotate POSTGRES_PASSWORD, set GRIMOIRE_API__SECRET_KEY,
# point GRIMOIRE_LLM__URL at your LLM endpoint.

# 4. Pull the security corpora.
mkdir -p /srv/grimoire/security-corpus
CORPUS_DIR=/srv/grimoire/security-corpus ./scripts/security/seed_corpus.sh

# 5. Start the security overlay.
docker compose -f docker-compose.yml -f docker-compose.security.yml up -d

# 6. Initialise the DB schema (one-time).
docker compose exec postgres pg_isready  # wait for healthy
uv run alembic upgrade head

# 7. Ingest the corpora.
uv run grimoire ingest --source-type sigma_rule  /srv/grimoire/security-corpus/sigma-rules/rules
uv run grimoire ingest --source-type mitre_attack /srv/grimoire/security-corpus/mitre-attack/enterprise-attack
uv run grimoire ingest --source-type nvd_cve     /srv/grimoire/security-corpus/nvd-cve/nvdcve-1.1-$(date -u +%Y).json
```

After ingest, you can smoke-test from the CLI:

```bash
uv run grimoire ask --severity high --tactic execution "powershell"
uv run grimoire search --source-type sigma_rule "lateral movement"
```

## Firewall

Hetzner Cloud Firewalls (or any equivalent — `ufw`, `nftables`) — open the
minimum surface:

| Port | Direction | Purpose |
|---|---|---|
| 22/tcp | inbound (tight source range) | SSH |
| 8001/tcp | inbound | Grimoire API (`GRIMOIRE_API__PORT`) |
| 5433/tcp | inbound | **Block.** Postgres should only be reachable from localhost / the docker network. |
| 8002/tcp | inbound | **Block.** ChromaDB — same. |
| 6379/tcp | inbound | **Block.** Redis — same. |

Example `ufw` set-up:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from <your-ip>/32 to any port 22 proto tcp
sudo ufw allow 8001/tcp                           # Grimoire API
sudo ufw enable
```

The compose overlay still publishes Postgres / Redis / Chroma ports on
the host (for ``alembic`` and debugging convenience). If you want them
fully internal, edit `docker-compose.security.yml` and drop the `ports:`
blocks.

## Backups

The overlay isolates state into three named volumes:
``postgres_data_security``, ``redis_data_security``,
``chromadb_data_security``. A daily backup that covers all three:

```bash
#!/usr/bin/env bash
set -euo pipefail
TS=$(date -u +%Y%m%dT%H%M%SZ)
DEST=/srv/backups/grimoire-${TS}
mkdir -p "${DEST}"

# Postgres — logical dump.
docker compose exec -T postgres pg_dump -U grimoire grimoire_security \
    | gzip > "${DEST}/postgres.sql.gz"

# Chroma — copy the volume.
docker run --rm \
    -v chromadb_data_security:/from:ro \
    -v "${DEST}":/to \
    alpine sh -c "cd /from && tar czf /to/chromadb.tgz ."

# Redis — RDB snapshot.
docker compose exec -T redis redis-cli --rdb /data/dump.rdb >/dev/null
docker run --rm \
    -v redis_data_security:/from:ro \
    -v "${DEST}":/to \
    alpine sh -c "cp /from/dump.rdb /to/redis.rdb"
```

Cron it at 03:00 UTC, ship to off-host storage (Hetzner Storage Box, S3,
or whatever you already use). Redis state is rebuildable (cache + rate
limit counters), so the Postgres dump + Chroma archive are the real
must-haves.

## Log locations

| Component | Location |
|---|---|
| Grimoire app | ``./logs/`` on the host (mounted into the container) |
| Postgres | ``./logs/postgres-security/`` |
| Container stderr | ``docker compose logs -f <service>`` |

Set ``GRIMOIRE_LOGGING__USE_JSON=true`` (default in
`.env.security.example`) for structured logs that ship cleanly into a
log aggregator.

## Smoke-test queries

After ingest, the following should succeed:

```bash
# CVE lookup — intent classifier routes through cve_lookup.
curl -s -H "Authorization: Bearer $API_KEY" \
     "http://localhost:8001/api/v1/query/search?severity=critical" \
     -d '{"query":"CVE-2024-0001","top_k":3}' \
     -H 'content-type: application/json' | jq

# Technique lookup — should boost MITRE rows.
curl -s -H "Authorization: Bearer $API_KEY" \
     "http://localhost:8001/api/v1/query/search?technique=T1059" \
     -d '{"query":"powershell","top_k":3}' \
     -H 'content-type: application/json' | jq

# Document listing filtered by indexed source_type.
curl -s -H "Authorization: Bearer $API_KEY" \
     "http://localhost:8001/api/v1/documents?source_type=sigma_rule&severity=high" | jq
```

## Maintenance

* **Corpus refresh:** re-run `seed_corpus.sh` weekly (it's idempotent —
  Sigma / MITRE pull as `git pull --ff-only`, NVD only re-downloads if
  the year-file is missing).
* **Schema migrations:** `uv run alembic upgrade head` after each
  Grimoire release.
* **API keys:** rotate `GRIMOIRE_API__SECRET_KEY` only when you can
  re-issue all downstream keys at the same time (changing the secret
  invalidates the hash-stored keys).

## See also

* [`docs/strategies/configuration.md`](../strategies/configuration.md) —
  every `settings.security.*` field.
* [`docs/strategies/usage.md`](../strategies/usage.md) — CLI + API filter
  recipes.
* [`docs/strategies/retriever.md`](../strategies/retriever.md) —
  intent classifier and re-rank math.
* [`docs/plans/security_strategy_plan.md`](../plans/security_strategy_plan.md)
  — phased implementation plan.
