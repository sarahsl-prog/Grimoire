# Changelog

All notable changes to Grimoire are documented in this file.

## [2.0.0] - 2026-06-18

### Added

- **MCP server** – Grimoire now exposes its full knowledge-base functionality via the Model Context Protocol (MCP) so AI assistants can query and manage documents natively.
  - `stdio` transport for local clients such as Claude Desktop, Cursor, and Windsurf.
  - `SSE` transport mounted at `/mcp` inside the existing FastAPI API server, plus a standalone `grimoire mcp --sse` server.
  - 14 tier-gated tools: `grimoire_search`, `grimoire_ask`, `grimoire_get_document`, `grimoire_list_documents`, `grimoire_list_categories`, `grimoire_watch_status`, `grimoire_status`, `grimoire_ingest_file`, `grimoire_ingest_directory`, `grimoire_generate`, `grimoire_create_category`, `grimoire_watch_start`, `grimoire_pg_query`, and `grimoire_delete_document`.
  - Tier-based access control:
    - `rdl` (Read) – search, ask, get/list docs/categories, watch_status, status.
    - `dvl` (Dev) – Read + ingest, generate, create_category, watch_start, pg_query.
    - `agt` (Agent) – Dev + delete_document.
- New CLI command: `grimoire mcp [--stdio|--sse --host HOST --port PORT]`.
- New CLI command group: `grimoire key create|list|revoke` for API-key management.

### Changed

- FastAPI app version synchronized to `2.0.0`.
- `pyproject.toml` now depends on `mcp>=1.8.0`.
- SSE MCP requests require an `X-API-Key` header validated by middleware before proxying to the MCP app.
- stdio MCP sessions require a valid `GRIMOIRE_API_KEY` environment variable validated at startup.

### Security

- `grimoire_pg_query` now wraps user-supplied SQL in a subquery and applies a bound `LIMIT` parameter using SQLAlchemy; non-SELECT/WITH statements and `SELECT INTO` are rejected by a Pydantic validator.
- MCP auth reuses the existing bcrypt-hashed API key system.

### Upgrading

1. Pull the new dependencies:
   ```bash
   uv sync
   ```
2. Apply any pending database migrations:
   ```bash
   uv run alembic upgrade head
   ```
3. Create an API key for your MCP client:
   ```bash
   # For full access (recommended for admin/Claude Desktop)
   grimoire key create --tier agent --name claude-desktop

   # For read-only assistants
   grimoire key create --tier read --name read-only-bot
   ```
4. Start the MCP server:
   - stdio:
     ```bash
     GRIMOIRE_API_KEY=grim_agt_... uv run grimoire mcp --stdio
     ```
   - standalone SSE:
     ```bash
     uv run grimoire mcp --sse --port 8100
     ```
   - SSE via the API server (already mounted at `/mcp`):
     ```bash
     uv run uvicorn grimoire.api.main:app --port 8001
     ```

See `README.md` for sample Claude Desktop / Cursor configurations.

## [1.x.x] - Previous releases

- Pre-2.0 history is available in the Git commit log.
