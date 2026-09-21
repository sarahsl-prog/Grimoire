# Upgrading an existing Grimoire install

This guide covers upgrading an **existing** deployment in place — code,
Python dependencies, database schema, container images, and configuration —
without losing ingested documents or embeddings.

For a first-time install, see the [Quick Start](../README.md#quick-start)
instead. For release-specific notes, read the `### Upgrading` block of every
release you are skipping in [CHANGELOG.md](../CHANGELOG.md), oldest first.

> **Rule of thumb:** Grimoire keeps state in three places — PostgreSQL
> (metadata, wiki pages, API keys), ChromaDB (vectors), and Redis (cache,
> rate-limit counters). Postgres and Chroma must stay in sync with each
> other and with your embedding model. Redis is disposable.

---

## 0. Check what you are running

```bash
cd /path/to/Grimoire

git rev-parse --short HEAD                      # current code revision
uv run grimoire --version                       # installed package version
uv run alembic current                          # applied schema revision
uv run grimoire status --detailed               # document / chunk counts
```

Write down the `alembic current` revision and the document count. Both are
your "did the upgrade work" baseline in step 6, and the revision is the
target if you have to roll back.

---

## 1. Back up first

Backups are cheap, re-ingesting 100K documents is not. Take all three
snapshots **before** stopping anything, and verify the dump is non-empty.

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
DEST=~/grimoire-backups/${TS}
mkdir -p "${DEST}"

# Postgres — logical dump (metadata, categories, wiki, API keys).
docker compose exec -T postgres pg_dump -U grimoire grimoire \
    | gzip > "${DEST}/postgres.sql.gz"

# ChromaDB — archive the named volume (vectors are not in Postgres).
docker run --rm \
    -v grimoire_chromadb_data:/from:ro \
    -v "${DEST}":/to \
    alpine sh -c "cd /from && tar czf /to/chromadb.tgz ."

# Configuration — .env holds secrets and is never in git.
cp .env "${DEST}/.env.bak"
[ -f grimoire.yaml ] && cp grimoire.yaml "${DEST}/grimoire.yaml.bak"

gzip -t "${DEST}/postgres.sql.gz" && ls -lh "${DEST}"
```

Volume names are prefixed with the Compose project name (the directory
name, lowercased). Confirm yours with `docker volume ls` — a security-mode
deploy uses `..._data_security` volumes, and the backup script in
[docs/deploy/hetzner_security.md](deploy/hetzner_security.md#backups) already
targets those.

Redis is not backed up on purpose: it holds cache entries and rate-limit
counters that rebuild themselves.

---

## 2. Stop anything that writes

Leaving a watcher or API server running during a migration is how you get
half-migrated rows.

```bash
# Stop the watcher, API server, and MCP server (Ctrl-C, or your
# systemd / supervisor unit):
sudo systemctl stop grimoire-api grimoire-watch    # if you run them as units

# Leave the databases up — Alembic needs Postgres in step 5.
```

Also quit any MCP client (Claude Desktop, Cursor, Windsurf) holding a stdio
session open against this install; it will happily keep ingesting otherwise.

---

## 3. Pull the new code

```bash
git fetch --all --tags
git checkout v2.1.0        # a release tag, if the release you want has one
# ...or track the default branch:
git pull --ff-only origin main
```

If you have local edits, `git stash` them first and reapply afterwards —
merge conflicts mid-upgrade are not a good time.

---

## 4. Update dependencies

```bash
uv sync
```

`uv sync` reconciles the virtual environment with `pyproject.toml`,
including **removals** and major-version bumps. Do not skip it because "the
app still starts" — a stale venv is the most common source of
post-upgrade `ImportError` / `AttributeError` noise. Grimoire 2.0.x moved
from `mcp` 1.x to 2.x, for example, and a venv still holding `mcp<2` fails
at MCP server startup rather than at install time.

If you use the optional extras, re-sync them too:

```bash
uv sync --extra mlflow        # observability
uv sync --extra dev           # tests, lint, mypy
```

---

## 5. Reconcile configuration

New releases add settings and occasionally rename them. Your `.env` is not
updated by `git pull`, so diff it against the shipped template:

```bash
diff <(grep -o '^[A-Z_]*' .env.example | sort -u) \
     <(grep -o '^[A-Z_]*' .env | sort -u)
```

Add any new keys you care about; the rest fall back to their defaults.

Two traps worth knowing:

- **Top-level extras are ignored, nested extras are not.** `Settings`
  ignores unknown top-level keys for forward compatibility, but every
  nested section (`llm`, `embeddings`, `database`, `vector_store`, …) is
  declared `extra="forbid"`. A key that was *removed* in the new release and
  still lives under a section of your `grimoire.yaml` will raise a
  validation error at startup, not a warning. Delete it.
- **Precedence is environment → `.env` → `grimoire.yaml` → defaults.** An
  exported shell variable silently wins over the file you just edited.

Verify the merged result before starting anything:

```bash
uv run grimoire config show          # secrets are redacted in this output
```

---

## 6. Update containers and apply migrations

```bash
docker compose pull                  # refresh postgres / redis / chromadb images
docker compose up -d
docker compose ps                    # wait for healthchecks to report healthy

uv run alembic upgrade head
uv run alembic current               # should now match `alembic heads`
```

Migrations are additive and each one ships a `downgrade()`, so a single-step
rollback is supported (see [Rolling back](#rolling-back)). Run them one
release at a time if you are jumping several versions — that is also the
order the CHANGELOG's `Upgrading` blocks assume.

A security-overlay deploy needs both files on every command:

```bash
docker compose -f docker-compose.yml -f docker-compose.security.yml pull
docker compose -f docker-compose.yml -f docker-compose.security.yml up -d
uv run alembic upgrade head
```

Note that `grimoire migrate` is **not** part of this flow — it moves vectors
from ChromaDB to Qdrant and has nothing to do with version upgrades.

---

## 7. Clear the cache and restart

Cached embeddings and query results can outlive the code that produced
them; drop them after any release that touches chunking, retrieval, or the
embedding model.

```bash
uv run grimoire cache clear
```

Then bring the services back:

```bash
uv run uvicorn grimoire.api.main:app --port 8001
uv run grimoire watch /path/to/documents        # if you run the watcher
GRIMOIRE_API_KEY=grim_agt_... uv run grimoire mcp --stdio
```

MCP clients cache the tool list from the handshake. Restart Claude Desktop
or Cursor after an upgrade that adds or renames tools, or they will keep
calling the old set.

---

## 8. Verify

```bash
uv run grimoire status --detailed          # counts should match step 0
curl -s localhost:8001/health | jq                      # {"status": "ok"}
curl -s localhost:8001/openapi.json | jq -r .info.version # should be the new version
uv run grimoire search "a phrase you know is indexed" --top-k 3
uv run grimoire key list                   # API keys survived the migration
```

Document and chunk counts that *dropped* mean something went wrong — stop
and restore from step 1 rather than re-ingesting on top of a damaged index.
If you run the test suite locally, `uv run pytest -m unit` is a fast
sanity check that the venv is coherent.

---

## Changing the embedding model

This is not an upgrade step, but upgrades are when people do it, and it is
the one change that invalidates your whole vector store.

`ChromaDBStore.initialize()` does not validate that an existing collection's
vectors match the configured model's dimension — a mismatch surfaces later
as an opaque Chroma error on insert or query, or (worse, same dimension,
different model) as quietly terrible search results. Changing
`GRIMOIRE_EMBEDDINGS__MODEL` therefore means re-embedding everything.

**`grimoire reindex` will not do this for you** — it only fills in chunk ids
the vector store is missing, so it treats every already-embedded chunk
(right model or wrong) as done and skips it. It's the right tool for a
Postgres/vector-store *count* drift (see `docs/deploy/docker.md`), not for
a model swap:

1. Back up (step 1) — you are about to destroy the vector store.
2. Either point `vector_store.chroma.collection_name` at a **new**
   collection, or delete the existing one:
   ```bash
   docker compose down
   docker volume rm grimoire_chromadb_data
   docker compose up -d
   ```
3. Set the new model in `.env`, then clear metadata so deduplication does
   not skip your files — the deduplicator hashes file content and will
   report unchanged documents as `skip`, leaving them unembedded.
4. Re-ingest the corpus:
   ```bash
   uv run grimoire ingest /path/to/documents --recursive --auto-tag
   ```

Budget for it: re-embedding is the slowest operation in Grimoire, and on a
large corpus it is an overnight job.

---

## Rolling back

If the new release misbehaves, roll back in the reverse order you upgraded:

```bash
uv run alembic downgrade <revision-from-step-0>   # e.g. 0006
git checkout <previous-tag-or-commit>
uv sync
uv run grimoire cache clear
docker compose up -d
```

If a downgrade fails or data looks wrong, restore the backup instead:

```bash
docker compose down
docker volume rm grimoire_postgres_data grimoire_chromadb_data
docker compose up -d postgres chromadb
gunzip -c ~/grimoire-backups/<TS>/postgres.sql.gz \
    | docker compose exec -T postgres psql -U grimoire -d grimoire
docker run --rm \
    -v grimoire_chromadb_data:/to \
    -v ~/grimoire-backups/<TS>:/from:ro \
    alpine sh -c "cd /to && tar xzf /from/chromadb.tgz"
cp ~/grimoire-backups/<TS>/.env.bak .env
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Target database is not up to date` | Migrations not applied | `uv run alembic upgrade head` |
| `Can't locate revision identified by '00xx'` | Code older than the database | Check out the newer code, or `alembic downgrade` before rolling back |
| Multiple heads in `alembic heads` | Two migrations branched from one parent | `uv run alembic history` to inspect, then merge — do not force-stamp |
| `ValidationError: Extra inputs are not permitted` | Removed setting still in `grimoire.yaml`/`.env` | Delete the stale key (see step 5) |
| `ImportError` / `AttributeError` in a vendored lib | Stale virtual environment | `uv sync` |
| MCP client shows old or missing tools | Client cached the handshake | Restart the client; re-check `GRIMOIRE_API_KEY` tier |
| `Unauthorized` from every API key after upgrade | `GRIMOIRE_API__SECRET_KEY` changed | Restore the old secret, or re-issue keys with `grimoire key create` |
| Chroma dimension / index errors | Embedding model changed | See [Changing the embedding model](#changing-the-embedding-model) |
| Search returns nothing, `status` shows documents | Vectors lost, metadata kept | Restore the Chroma volume from backup |

---

## See also

- [CHANGELOG.md](../CHANGELOG.md) — per-release `Upgrading` notes
- [docs/deploy/hetzner_security.md](deploy/hetzner_security.md) — security-mode
  deploy, backups, and maintenance
- [docs/strategies/configuration.md](strategies/configuration.md) — every
  `settings.security.*` field
