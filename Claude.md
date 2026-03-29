
# Claude.md - template

# Project overview
- Python 3.12, FastAPI API backend, PostgreSQL, Redis.
- Main app code in `app/`, tests in `tests/`, infra scripts in `infra/`. 

## Workflow
- Start complex tasks in Plan mode
- Get plan approval before implementation
- Break large changes into reviewable chunks

# Python package management
## uv (preferred)
Use **uv** for all new work in this project.

- Install dependencies: `uv add <package>`
- Remove dependencies: `uv remove <package>`
- Sync from pyproject/lock: `uv sync`
- Run a script: `uv run path/to/script.py`
- Run tools: `uv run pytest`, `uv run ruff`, `uv run ruff format .`

# Coding style (Python)
- Use type hints everywhere; code must be `mypy`-friendly.
- Format with `black` (88 chars), use f-strings, no wildcard imports.
- Use pydantic models for external I/O, FastAPI `Depends` for DI.
- Prefer async endpoints and non-blocking libraries. 
- well commented modular code
- use loguru and setup logging for the main processes in a /log directory or /var/log/xxx on Linux

# Commands Claude should use
- Install deps: `uv pip install`
- Run app: `uv run uvicorn app.main:app --reload`
- Run tests: `uv run pytest -q`
- Lint: `uv run ruff check .`
- Format: `uv run black app tests`

# Security & performance
- Never log secrets or tokens.
- Use parameterized DB queries only.
- Validate all external inputs with pydantic.
- Prefer async DB access and Redis caching where appropriate.

# Testing & quality
- Use `pytest` for tests, `pytest -q` for quick runs.
- Add tests for all new public functions; keep coverage high.
- Run `ruff check .` and `black` before com

## Verification Requirements
- Run `npm test` after code changes
- Run `npm run typecheck` before marking complete
- For API changes, test with `curl` or Postman
- For UI changes, verify in browser before committing





