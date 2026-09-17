# Containerize the Grimoire App — Design

**Date:** 2026-09-17
**Status:** Approved for planning
**Scope:** Package the Grimoire application (API, MCP server, watcher, CLI) as a
container image and define a Compose topology that runs the whole stack in any
environment.

---

## Problem

Grimoire's infrastructure dependencies already run in Docker
(`docker-compose.yml` defines postgres, redis, chromadb, plus optional pgadmin
and redis-commander). The application itself does not. It runs from a local
`.venv` on one WSL2 host, pinned to that machine's Python version, its installed
CUDA stack, and a `grimoire.yaml` full of `localhost` endpoints.

This blocks three things:

1. **Running the MCP server as a managed service.** The original request. Today
   the MCP server is started by hand or by an editor spawning a stdio process.
2. **Deploying to any other environment.** A second host needs the exact Python
   version, the `uv` toolchain, system libraries for `python-magic` and Docling's
   OCR stack, and a hand-reconstructed config.
3. **Running components independently.** API, MCP, and the watcher share one
   process lifecycle today, so there is no way to restart one without the others.

## Non-Goals

- Kubernetes manifests or Helm charts. Compose is the target.
- Multi-host orchestration, service mesh, or autoscaling.
- Containerizing Postgres/Redis/Chroma differently than they already are.
- Migrating existing local data. The corpus is re-ingested from source
  documents against the new stack (see *Data*).
- Celery workers. `celery` appears in `pyproject.toml` and
  `grimoire/config/settings.py` but no task, worker, or queue exists in
  `grimoire/`. No worker container is defined. Removing the dead config is out
  of scope for this work.

---

## Prerequisite: MCP SSE Authentication Gap

This must be fixed before any MCP port is published, and is therefore the first
task in the implementation plan rather than a follow-up.

**The gap.** `grimoire/mcp/router.py:37` defines `mount_mcp()`, which wraps the
MCP ASGI app in middleware that validates the `X-API-Key` header against the
bcrypt-hashed key store before forwarding the request. The FastAPI app uses this
path: `grimoire/api/main.py:78` calls `mount_mcp(app, path="/mcp")`.

The standalone SSE transport does not. `grimoire/cli/mcp.py:48` passes
`mcp_server.sse_app()` straight to uvicorn with no wrapper, so
`grimoire mcp --sse` serves MCP with no authentication at all.

**Why it matters.** `grimoire/mcp/tools.py` registers roughly seventeen tools.
Nine call `require_tier()`, which resolves the current key through
`get_current_api_key()` and raises when the context variable is unset — those
fail closed. The remaining tools have no tier guard:

- `grimoire_search`
- `grimoire_search_cve`
- `grimoire_search_playbook`
- `grimoire_ask`
- `grimoire_get_document`
- `grimoire_list_documents`
- `grimoire_list_categories`
- `grimoire_status`

On the unauthenticated transport, any client that can reach the port gets read
access to the entire corpus — including the security corpus of CVE, Sigma, and
IR playbook content — and can run arbitrary LLM queries against it. Publishing
this port from a container turns a local-only oversight into a network-reachable
one.

**The fix.** Factor the ASGI auth middleware out of `mount_mcp()` into a shared
helper, then route the standalone SSE path through the same helper. Both
transports end up sharing one authentication implementation, and no code path
serves MCP unauthenticated. This is covered by its own test and its own commit,
landing before the Dockerfile.

---

## Architecture

### One image, role selected by command

A single multi-stage `Dockerfile` at the repository root.

The **builder** stage starts from `python:3.13-slim`, installs `uv`, and
resolves dependencies into `/opt/venv` from `pyproject.toml` and the existing
`uv.lock`, so image builds are reproducible and match the host environment.

`uv.lock` pins `torch==2.11.0` from PyPI, which on Linux is the CUDA build and
pulls in fifteen `nvidia-*` packages — 1.2GB of torch plus 2.7GB of CUDA
libraries, all of it dead weight on a host without a GPU. The builder therefore
takes a `TORCH_VARIANT` argument. The default `cpu` variant removes the CUDA
packages and reinstalls the same torch version from the PyTorch CPU index; the
`gpu` variant keeps what the lock resolved. Both variants pin the identical
torch version, so behavior does not diverge between them.

The **runtime** stage starts from the same slim base, copies `/opt/venv` and the
`grimoire` package, installs only the system libraries the application needs at
runtime (`libmagic1` for `python-magic`, plus Docling's OCR requirements), and
creates a non-root `grimoire` user that owns the writable paths.

Every role runs this one image and differs only in its `command`. The tradeoff
is accepted deliberately: the MCP and API containers carry Docling and torch
even though only ingestion uses them. Splitting the dependency graph is not
currently possible without restructuring the package, and one image means one
build, one layer cache, and one artifact to patch when a CVE lands in a
transitive dependency.

**Model cache.** Docling and `sentence-transformers` download model weights on
first use — `all-mpnet-base-v2` plus the OCR models pulled in by
`parse_pdf_ocr: true` and `parse_images: true`. A named `model_cache` volume
mounts at the `grimoire` user's cache directory so those downloads happen once
per environment rather than once per container start.

### Services

`docker-compose.yml` gains four application services beside the existing
infrastructure:

| Service | Command | Notes |
|---------|---------|-------|
| `db-migrate` | `alembic upgrade head` | One-shot. `restart: no`. Gates the rest. |
| `api` | `uvicorn grimoire.api.main:app --host 0.0.0.0 --port 8001` | Healthcheck against `/health` (`grimoire/api/main.py:80`). Also serves authenticated MCP at `/mcp`. |
| `mcp` | Thin FastAPI app, SSE on 8100 | Dedicated MCP service. See below. |
| `watcher` | `grimoire watch start` | Read-only bind mount of the watched corpus. |

The service is named `db-migrate`, not `migrate`, to avoid implying a
relationship with the `grimoire migrate` CLI command. That command migrates data
*between vector stores* and is currently a stub that logs "no migration backend
is implemented" (`grimoire/cli/migrate.py:45`). Schema migration is Alembic, and
is invoked directly.

`alembic/env.py:45` resolves the database URL through `get_settings()` rather
than from `alembic.ini`, so the container's `GRIMOIRE_DATABASE__URL` applies to
migrations automatically and the hardcoded `sqlalchemy.url` in `alembic.ini` is
ignored. No Alembic configuration changes.

**Startup ordering.** `api`, `mcp`, and `watcher` each declare
`depends_on: { db-migrate: { condition: service_completed_successfully } }`, and
`db-migrate` in turn waits on the Postgres healthcheck. Schema migration
therefore completes exactly once before any application process opens a
connection, and no application container races another to run Alembic.

**The MCP service.** A minimal FastAPI application that does nothing but call
the existing `mount_mcp(app, "/mcp")`. It reuses the authentication middleware
that already protects the REST-mounted endpoint, so the dedicated MCP service
requires no new authentication code — only the refactor described in the
prerequisite. The service runs on its own port and restarts independently of the
API, which was the point of giving it a container.

### ChromaDB becomes live

The `chromadb` service in `docker-compose.yml` is currently dead: it starts,
persists nothing useful, and no client connects to it. The application uses an
embedded `PersistentClient` against the `./chroma_db` directory.

`grimoire/vectorstore/chromadb.py` already implements both paths — line 131
constructs an `HttpClient` when a host is configured, line 143 falls back to
`PersistentClient`. Setting `GRIMOIRE_VECTOR_STORE__HOST=chromadb` and
`GRIMOIRE_VECTOR_STORE__PORT=8000` in the container environment selects the
HTTP client with no code change.

This also removes a correctness hazard. Embedded Chroma is single-writer. With
`api` and `watcher` as separate containers both able to write, sharing an
embedded store across them would risk corruption. A single Chroma server
serializes those writers properly.

### Configuration

Bare-metal and container workflows both stay supported, so configuration is
layered rather than rewritten:

- `grimoire.yaml` keeps its `localhost` defaults. `uv run grimoire ask` on the
  host continues to work exactly as it does today.
- Compose supplies `GRIMOIRE_*__*` environment variables that override those
  defaults with service DNS names (`postgres`, `redis`, `chromadb`).

The delimiter and precedence already exist; `grimoire/config/settings.py`
documents `GRIMOIRE_LLM__MODEL` as the nested-override form. No settings code
changes.

**Ollama.** Compose sets `extra_hosts: ["host.docker.internal:host-gateway"]`
and `GRIMOIRE_LLM__URL=http://host.docker.internal:11434`, so containers reach
the Ollama daemon on the host. That preserves the current setup: already-pulled
weights, the `kimi-k2.6:cloud` and `ornith:9b` models, and the cloud API key the
daemon holds. Because the endpoint is a plain environment variable, another
environment can point at a remote or containerized Ollama without touching an
image. An optional `ollama` Compose profile is provided for fully self-contained
deployments.

Note that the `/v1` suffix must not appear in this URL. The agents call Ollama's
native endpoint by appending `/api/generate` (`grimoire/agents/query.py:422`,
`grimoire/agents/content_gen.py:431`, `grimoire/agents/coordinator.py:820`), so a
`/v1` base yields `/v1/api/generate` and a 404. The `.env.example` container
block calls this out.

### Overlays

Following the precedent already set by `docker-compose.security.yml`:

- **`docker-compose.gpu.yml`** — enables GPU embedding. Because the base image
  ships CPU-only torch, a device reservation alone would do nothing: the overlay
  must also rebuild the image with the CUDA torch variant, which it does through
  the `TORCH_VARIANT=gpu` build argument, tagging the result `grimoire:gpu` so
  the two variants never overwrite each other. It then reserves an nvidia device
  and sets `GRIMOIRE_EMBEDDINGS__DEVICE=cuda`. The host has an RTX 2000 Ada and
  the nvidia container runtime installed, so this is testable locally. The base
  image stays CPU-only and portable; only hosts that opt in pay for CUDA.
- **`docker-compose.dev.yml`** — bind-mounts the source tree and runs uvicorn
  with `--reload` for edit-and-refresh development.

### Data

Volumes start empty and the corpus is re-ingested from source documents. No
migration path is built from the embedded `./chroma_db` store into the Chroma
service.

This costs a full re-embed, and it buys an end-to-end exercise of the
containerized ingestion pipeline — parsing, chunking, embedding, tagging, and
dual-write to Postgres and Chroma — which is the part of the system most likely
to break under containerization and the part a migration script would skip.

---

## Testing

Tests extend `tests/deploy/`, following the approach already established by
`tests/deploy/test_compose_overlay.py`: parse the Compose and Docker files and
assert their properties without invoking Docker. Those tests are fast, run in
any CI environment, and catch the class of error that actually occurs here —
a service pointing at the wrong host, a missing dependency edge, an overlay that
silently changes more than intended.

**Auth regression (prerequisite).** A unit test asserting that the standalone
SSE application rejects a request carrying no `X-API-Key` header, and that an
invalid key is rejected too. This is the test that must fail before the
prerequisite fix and pass after it.

**Compose structure.** The `api`, `mcp`, `watcher`, and `db-migrate` services
exist; `db-migrate` declares `restart: no`; the three long-running services each
gate on `db-migrate` completing successfully; `db-migrate` gates on the Postgres
healthcheck.

**No leaked localhost.** No application service's environment points at
`localhost` for Postgres, Redis, or Chroma — the failure mode that produces a
container which starts cleanly and then cannot reach anything.

**Chroma wiring.** Application services set `GRIMOIRE_VECTOR_STORE__HOST` and
`__PORT`, which is what selects the `HttpClient` branch.

**Ollama URL.** The configured LLM URL resolves through `host.docker.internal`
and carries no `/v1` suffix.

**GPU overlay containment.** Merging `docker-compose.gpu.yml` adds device
reservations and changes nothing else — no altered ports, volumes, or commands.

**Dockerfile properties.** A final `USER` instruction that is not `root`; no
secret-bearing `ARG` or `ENV`; the CPU torch index present in the builder stage.

**Lint.** `docker compose config` validates every overlay combination, and
`hadolint` checks the Dockerfile. Both run as part of the deploy test suite.

---

## Documentation

- **`README.md`** — a Docker deployment section covering the one-command
  startup, and the SSE MCP client configuration that replaces today's local
  process invocation.
- **`docs/deploy/docker.md`** — new. Image build, service topology, the full
  environment variable reference, overlay usage for GPU and development, volume
  and backup notes, and troubleshooting for the failures this stack actually
  produces: Ollama unreachable from a container, Chroma connection refused,
  migration ordering, and model-cache cold starts.
- **`.env.example`** — a container configuration block, including the `/v1`
  warning.
- **`.vscode/mcp.json`** — add a `grimoire` server entry pointing at the SSE
  endpoint. The file currently contains only an unrelated `backboard-docs`
  server, so Grimoire's own MCP server has never been registered there.
- **`CHANGELOG.md`** — the containerization entry and the MCP authentication
  fix, the latter called out as a security fix.

---

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Ollama reachability | Configurable, default host-gateway | Preserves existing models and cloud key; one env var retargets it |
| Vector store | Switch to the Chroma service | `HttpClient` already implemented; embedded store is single-writer |
| Torch | CPU-only base, GPU overlay | Portable base image; GPU hosts opt in |
| MCP transport | SSE over HTTP | The stated goal — a real service, reachable remotely |
| MCP auth | Thin FastAPI app reusing `mount_mcp` | No new auth code; one implementation for both transports |
| SSE auth gap | Fixed in this plan | Containerization is what would expose it |
| Image layout | One image, role by command | One build and one patch surface; deps are not cleanly separable today |
| Services | `api`, `mcp`, `watcher`, `db-migrate` | Independent restarts; migration ordering made explicit |
| Existing data | Start fresh, re-ingest | Exercises the containerized pipeline end to end |
| Bare-metal workflow | Stays first-class | `grimoire.yaml` defaults unchanged; containers override via env |
