# Docker deploy — Grimoire application

This is the general-purpose guide to running Grimoire under Docker Compose:
the base stack (`docker-compose.yml`) plus the GPU and development overlays.
If you're deploying the security-domain corpus pipeline specifically, see
[`docs/deploy/hetzner_security.md`](hetzner_security.md) instead — it covers
the same container image with a different overlay and seeded corpora.

## Prerequisites

- Docker Engine with the Compose v2 plugin (`docker compose version` should
  print `v2.x`, not the standalone `docker-compose` v1 binary).
- Roughly 10GB of free disk: the built image alone is about 3.5GB, and the
  `model_cache` volume adds several more once Docling and sentence-transformers
  have downloaded their weights. Budget extra for the `postgres_data` and
  `chromadb_data` volumes as your corpus grows.
- Ollama reachable from the host, listening on its default port (11434).
  Containers reach it through `host.docker.internal` by default — see
  [Ollama](#ollama) below if that doesn't work for your setup.

## Quick start

```bash
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD and any other credentials you want to
# change from the example defaults.
docker compose up -d --build
curl http://localhost:8001/health
```

The first `up -d --build` builds the `grimoire:latest` image, starts
Postgres, Redis, and ChromaDB, runs the one-shot `db-migrate` service to
bring the schema current, then starts `api`, `mcp`, and `watcher`. The health
check should return `{"status":"ok"}` once `api` has finished its startup
sequence (embedding model load can take a minute on a cold `model_cache`).

## Services

| Service | Port | Purpose |
|---|---|---|
| `postgres` | 5434→5432 | Primary metadata database (Postgres 16) |
| `redis` | 6379 | Cache, rate limiting, Celery broker/result backend |
| `chromadb` | 8060→8000 | Vector database |
| `db-migrate` | — | One-shot Alembic migration; every app container gates on its success and does not start until it completes |
| `api` | 8001 | REST API (Swagger UI at `/docs`); also mounts authenticated MCP at `/mcp/sse` |
| `mcp` | 8100 | Dedicated MCP server, restartable independently of the REST API |
| `watcher` | — | Filesystem watcher; ingests from the host directory mounted at `/data/watch` |
| `pgadmin` (profile `tools`) | 5050 | Optional Postgres management UI |
| `redis-commander` (profile `tools`) | 8081 | Optional Redis management UI |

The `tools` profile services don't start with a plain `docker compose up`;
add `--profile tools` to bring them up alongside the rest.

## Configuration

Settings resolve in this precedence order (highest wins), per
`settings.py:1107`: environment variables, then `.env`, then `grimoire.yaml`,
then built-in defaults. Inside a container, "environment variables" means the
compose `environment:` block — see the warning below.

The full set of `GRIMOIRE_*` variables the compose file sets for every app
container lives in the `x-grimoire-env` anchor at the top of
`docker-compose.yml`: database URL, vector store host/port, Redis host/port,
Celery broker/result URLs, log directory, cache path, upload directory, and
the Ollama URL. `.env.example` documents the complete list of variables the
application understands, most of which you'd only override for non-default
behavior (embeddings, chunking, auth, wiki, etc.) — see that file for the
full reference table.

`GRIMOIRE_API__UPLOAD_DIR` (containers: `/app/uploads`, set in
`x-grimoire-env`) and `GRIMOIRE_API__MAX_UPLOAD_BYTES` (default
`104857600`, 100 MB) control `POST /ingest/upload`: where it stages
incoming files and how large a single upload may be before it's rejected
with a 413.

> **A mounted `.env` is not read inside containers.** `settings.py:1049`
> resolves the dotenv path relative to the *installed package*, and inside
> the image that's `/opt/venv/lib/python3.13/site-packages/grimoire/...` —
> there is no `.env` there, mounted or otherwise. Configuration must reach
> containers as real environment variables. The compose file handles this by
> declaring `env_file: .env` on every app service (which Compose reads on
> the host and injects as environment variables) and by setting
> container-specific values explicitly in `x-grimoire-env`. If you add a new
> setting, either put it in `x-grimoire-env` or confirm it's read from `.env`
> on the host side, not expect the app to open the mounted file itself.

**Two Ollama variables, on purpose.** `.env.example` ships both
`GRIMOIRE_LLM__URL` (line 46, `http://localhost:11434` — correct for running
the app bare-metal on the host) and `GRIMOIRE_OLLAMA_URL` (line 202,
`http://host.docker.internal:11434` — read by `docker-compose.yml` on the
host and injected into containers as `GRIMOIRE_LLM__URL`). After
`cp .env.example .env`, both variables are present; the compose
`environment:` block always wins for containers, so `GRIMOIRE_OLLAMA_URL` is
the one that actually takes effect there. This is harmless but easy to
mistake for a bug — if you're editing the Ollama target for a container
deployment, edit `GRIMOIRE_OLLAMA_URL`, not `GRIMOIRE_LLM__URL`.

## Ollama

Containers default to `host.docker.internal:11434` because the app doesn't
ship a bundled Ollama container — you're expected to run Ollama on the host
(or point at a remote/containerized instance) and let the app reach it
through the Docker bridge. `extra_hosts: host.docker.internal:host-gateway`
in `docker-compose.yml` is what makes that hostname resolve from inside the
containers.

To retarget Ollama — a remote box, a containerized daemon on the same host,
or a different port — set `GRIMOIRE_OLLAMA_URL` in `.env`. It must **not**
end in `/v1`: the agents append `/api/generate` themselves, so a `/v1` base
produces `/v1/api/generate`, which 404s.

## Overlays

Two overlays extend the base compose file; apply them with `-f` in sequence
(later files override earlier ones):

```bash
# GPU — rebuilds the image with the CUDA torch variant, tagged grimoire:gpu
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build

# Development — bind-mounts ./grimoire over the installed package and
# enables uvicorn's --reload, so source edits take effect without a rebuild
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

The GPU overlay requires the NVIDIA Container Toolkit on the host; the base
image is CPU-only torch, so reserving a device isn't enough on its own — the
overlay rebuilds the image (`TORCH_VARIANT=gpu`) to get CUDA-linked torch and
torchvision, then reserves the GPU and sets `GRIMOIRE_EMBEDDINGS__DEVICE=cuda`.

There's also a security-domain overlay (`docker-compose.security.yml`) for
the Sigma/CVE/MITRE corpus pipeline — see
[`docs/deploy/hetzner_security.md`](hetzner_security.md).

> **Adding a fourth overlay?** `.gitignore` blanket-ignores
> `docker-compose.*.yml` (to keep ad hoc local overrides out of git) and then
> explicitly un-ignores the security, GPU, and dev overlays with `!` negation
> lines. A new overlay file needs its own negation line or it will be
> silently untracked.

## Volumes and backup

| Volume | Holds | Back up? |
|---|---|---|
| `postgres_data` | All document metadata, categories, wiki pages, API keys | Yes — the source of truth |
| `chromadb_data` | Vector embeddings | Yes — expensive to regenerate at scale |
| `model_cache` | Downloaded Docling and sentence-transformers model weights | No — rebuildable, just re-downloads on next cold start |
| `app_cache` | Grimoire's internal query/embedding cache | No — rebuildable |
| `app_uploads` | Files uploaded through the GUI or `POST /ingest/upload` | **Yes** — the only copy of every uploaded document |
| `app_logs` | Application logs | Optional, not required for recovery |

`app_uploads` is the one application volume that is not rebuildable.
`POST /api/v1/ingest/upload` stages each uploaded file there and records that
path as the document's `source_path`, so the staged file is the original —
deleting the volume orphans every row that came in through the GUI.

A logical Postgres dump plus a tar of the ChromaDB volume covers a full
recovery; see the backup script in
[`docs/deploy/hetzner_security.md`](hetzner_security.md#backups) for a
concrete example (written against the security overlay's volume names, but
the same approach applies to `postgres_data` and `chromadb_data` here).

**`./chroma_db` in the repo root is the legacy embedded vector store** from
running Grimoire bare-metal on the host, before containerization. It is a
plain directory, not a Docker volume, so `docker compose down -v` doesn't
touch it — but the containerized stack never reads it: containers talk to
the `chromadb` service over HTTP (port 8000 internally, `chromadb_data`
volume), not a local path. Once you've re-ingested your corpus into the
containerized stack and confirmed the `chromadb` service has the vectors,
`./chroma_db` is safe to delete.

**Running `grimoire ingest` / `grimoire search` etc. bare-metal on the host
silently uses the embedded `./chroma_db` instead of the shared containerized
service.** `GRIMOIRE_VECTOR_STORE__HOST`/`GRIMOIRE_VECTOR_STORE__PORT` are
only set inside containers (via `x-grimoire-env`); a host shell has neither,
so `VectorStoreConfig.host` defaults to `None` and the app falls back to a
per-process embedded client at `CHROMADB_PATH`. The document row still lands
in the shared Postgres (host and containers point at the same instance via
`POSTGRES_PORT`), so `grimoire docs list` looks correct — but the embeddings
are invisible to `api`/`mcp`, so containerized search/query finds nothing for
that document. Set `GRIMOIRE_VECTOR_STORE__HOST=localhost` and
`GRIMOIRE_VECTOR_STORE__PORT=<CHROMADB_PORT>` in `.env` so host-side commands
target the same service over its host-exposed port, or always ingest through
a container (`docker compose exec watcher grimoire ingest ...`, since
`watcher` is the one service with `./documents` mounted).

Run `grimoire status --detailed` to check for exactly this: it prints which
vector store backend is active (`chromadb (embedded, path=...)` vs
`chromadb (remote host:port)`) and compares the Postgres chunk count against
the live embedding count in that backend, flagging a `WARNING` when they
drift apart. The same `--detailed` run also reports cache backend stats
(disk or Redis, whichever `GRIMOIRE_CACHE__STORAGE` is set to).

If it does flag a drift, fix the `GRIMOIRE_VECTOR_STORE__HOST`/`PORT`
mismatch first, then run `grimoire reindex` — it re-embeds just the chunks
missing from the (now correctly targeted) vector store, using the chunk
text already in Postgres, instead of re-ingesting the whole corpus. Use
`grimoire reindex --dry-run` first to see what it would touch.

## Troubleshooting

**`grimoire status` (or any CLI command) fails with `ConnectionRefusedError` on
`127.0.0.1:<POSTGRES_PORT>`, and `docker compose ps` only shows `api`, `mcp`,
`watcher`, and `db-migrate`.** You brought up an overlay file alone — e.g.
`docker compose -f docker-compose.gpu.yml up -d --build` — instead of the
two-file form. `docker-compose.gpu.yml` only re-declares `api`, `mcp`,
`watcher`, and `db-migrate` (to swap their image/build args); it never
defines `postgres`, `redis`, or `chromadb`, so those three are silently
never created. Compose does not warn about this — it just builds and starts
whatever services the given files define. Fix: always chain the base file
with `-f`:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

**LLM generation fails with a 404.** Almost always a `/v1` suffix on the
Ollama URL — check `GRIMOIRE_OLLAMA_URL` in `.env`. If the URL is correct,
confirm the host's Ollama daemon is actually listening on the Docker bridge
interface, not just `127.0.0.1`; by default Ollama binds to localhost only,
which containers can't reach through `host.docker.internal`. Check this
before it bites a real query: `grimoire status --detailed` pings
`<GRIMOIRE_OLLAMA_URL>/api/tags` and reports `Ollama (<model>): reachable`
or `unreachable at <url> (...)`.

**Chroma connection refused.** Either the `chromadb` service is unhealthy
(`docker compose ps chromadb`; check `docker compose logs chromadb`), or
`GRIMOIRE_VECTOR_STORE__HOST` isn't set, in which case the app silently falls
back to an embedded (in-process) Chroma client instead of the service — the
compose file sets this for you, but if you're overriding `environment:`
locally, make sure that variable survives. `grimoire status --detailed`
reports `Vector store: unreachable (...)` when the configured backend can't
be reached at all.

**App containers exit immediately at first start.** Check `db-migrate`
first — `db-migrate` runs `alembic upgrade head` once, and `api`, `mcp`, and
`watcher` all gate on `db-migrate` completing successfully
(`depends_on: db-migrate: condition: service_completed_successfully`). If the
migration failed, every app container will exit right after without ever
really starting. `docker compose logs db-migrate` has the actual error.

**Restarting a single service doesn't pick up dependency state.**
`docker compose restart api` and `docker compose up --no-deps api` do
**not** re-evaluate `depends_on` — Compose only checks dependency conditions
on the initial `up`. So if you restart `api` alone while `db-migrate` hasn't
rerun, or before Postgres reports healthy again after a host reboot, `api`
comes back up regardless and may fail to connect. This is normal Compose
behavior, not a bug in this stack: run `docker compose up -d` (no service
name) if you want dependency ordering re-applied.

**First ingestion is very slow.** Expected on a cold `model_cache` — Docling
and sentence-transformers are downloading model weights on first use.
Subsequent ingests are fast once the volume is warm.

**`chromadb` shows `unhealthy`.** Check `docker compose logs chromadb` for
the cause.

**Host port already in use for `chromadb`.** `CHROMADB_PORT` (default 8060
in the compose file, but frequently overridden to 8000 in a local `.env`)
can collide with an unrelated container or process already bound to that
port on the host — Compose's error (`Bind for 127.0.0.1:PORT failed: port is
already allocated`) doesn't say what's using it. Check with `ss -ltnp | grep
<port>` and `docker ps --format '{{.Names}}\t{{.Ports}}'`. Override for a
single run without touching `.env`: `CHROMADB_PORT=8060 docker compose up
-d`. Internal container-to-container traffic always uses port 8000 on the
`grimoire-network` regardless of this mapping, so changing it is safe.

**Rebuilding the image can fail with a `uv` download timeout on a slow
network.** `uv sync`'s per-request timeout defaults to 30s
(`UV_HTTP_TIMEOUT`), and the CUDA build of `torch` plus its `nvidia-*`
dependencies total 2.5GB+ before the CPU swap step even runs. On a
constrained connection this reliably fails partway through with `Failed to
download ... due to network timeout`, on a different package each attempt
since nothing is cached between failed `RUN` layers. If you have a working
`grimoire:latest` image already and the source tree hasn't changed, prefer
`docker compose up -d` (no `--build`) over forcing a rebuild.

**Watcher and inotify across the WSL2 bind mount.** This is a known WSL2
weakness in general — inotify events don't always propagate across a
Windows-filesystem bind mount. In practice, on this stack, a file copied
into the watched `documents/` directory *was* detected and ingested
correctly within the test window (event logged within seconds, ingestion
completed in under 30s). If you find the watcher isn't picking up new files
on your setup, don't assume it's broken before checking: `docker compose
exec watcher grimoire watch start /data/watch --poll-interval 30` (or the
compose `command:` override) switches to polling instead of relying on
inotify, at the cost of a delay up to the poll interval.

**`uv run pre-commit run --all-files` fails immediately with
`.pre-commit-config.yaml is not a file`.** The file doesn't exist anywhere
in this repository's history — `pre-commit` is listed as a dependency in
`pyproject.toml`, but no config was ever committed. Run the tools directly
instead until a config is added: `uv run ruff check .`, `uv run black
--check .`, `uv run mypy grimoire/`, `uv run bandit -r grimoire/`. As of this
verification pass, `ruff check .` alone reports 173 pre-existing findings
(mostly `S108` temp-path warnings in test fixtures), unrelated to this
branch.

**`uv run pytest -v` has 5 pre-existing failures unrelated to containerization.**
`test_mcp_mlflow.py::test_trace_mcp_tool_wraps_when_active`,
`test_parser.py::TestDocumentParserSupportedFormats::test_unsupported_txt`,
`test_parser.py::TestDocumentParserAsync::test_parse_unsupported_format`,
`test_storage_gdrive.py::TestGoogleDriveAdapterHappyPath::test_save_tokens`,
and `test_storage_onedrive.py::TestTokenPersistence::test_tokens_saved_after_authentication`
all fail identically on `main` — none of the files involved were touched by
this branch. No regression from the MCP transport change in Task 1; the
1653 other tests pass.
