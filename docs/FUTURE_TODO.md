# Future TODO — Lint/Type Cleanup Follow-ups

Generated from the `proj-updates` ruff + mypy remediation pass (2026-09-12).
`mypy grimoire/` is clean; `ruff check .` is down to 169 (from 222). The items
below were deliberately left unfixed because each needs an owner decision,
not a mechanical lint fix.

## 1. `StrEnum` migration (ruff UP042, ~29 sites)

Ruff wants `class X(str, Enum)` rewritten as `class X(StrEnum)` (Python 3.11+).
Affected: `grimoire/db/models.py`, `grimoire/config/settings.py`, and other
enum definitions across the codebase.

**Why deferred:** `StrEnum.__str__` returns the bare value (`"active"`),
while `(str, Enum)` returns `"ClassName.MEMBER"` unless `__str__` is
overridden. This can silently change serialized output anywhere an enum is
interpolated into a string, logged, or written to the DB/JSON without
`.value`. Needs a deliberate migration: audit every `str(x)` / f-string use
of each enum, migrate one enum class at a time, and re-run the full test
suite + a data spot-check per model.

## 2. Deferred imports in CLI entrypoint (ruff E402, 13 sites)

`grimoire/cli/main.py:49-60` — imports placed after module-level code on
purpose, to keep CLI startup fast (heavy deps like docling/torch only
imported when their subcommand is invoked).

**Action:** add a `per-file-ignores` entry for `E402` on this file in
`pyproject.toml` rather than reordering imports (reordering would import
the heavy deps eagerly and undo the startup-time optimization).

## 3. Real complexity refactors (ruff C901, 7 sites)

Functions that exceed the complexity threshold for real reasons, not lint
noise:

- `_process_docling_result` (parser) — complexity 20
- `_build_rule_text` (security chunker) — complexity 20
- `parse_nvd_json` (NVD/CVE parser) — complexity 18
- 4 more, see `ruff check . --select C901` for current list/line numbers

**Action:** these need actual decomposition (extract helper functions per
branch/format handled), not a suppression. Treat as small refactor tasks,
one function per PR, with test coverage for each branch before refactoring
(systematic-debugging / TDD approach — characterize current behavior first).

## 4. Bandit-style false positives (ruff S105/S603/S104/S108, 6 sites)

E.g. `TOKEN_URL` constants flagged as "possible hardcoded password" (S105)
because the name matches a heuristic, when it's actually a URL string.

**Action:** add targeted `# noqa: S105` (or equivalent) comments at each
site with a one-line reason, rather than a blanket rule disable — keeps
bandit/ruff useful for catching real future secrets.

## 5. Remaining pure style (~18 sites)

`SIM101`, `SIM102`, `SIM103`, `SIM108`, `SIM118`, `N802`, `N806`, `N811`,
`UP007`, `UP047`. No behavior risk — safe to fix opportunistically or in a
single low-risk cleanup pass. Lowest priority of this list.

## 6. mypy `python_version` mismatch

`pyproject.toml` sets `[tool.mypy] python_version = "3.12"`, but
`CLAUDE.md` states the project targets Python 3.13. A few modules already
use PEP 695 generic syntax (`class BaseRepository[T]`,
`def log_execution[**P, T]`), which type-checks fine under 3.12+ so nothing
is currently broken — but the config should match the stated target.

**Action:** bump to `python_version = "3.13"`, re-run
`mypy grimoire/` in full, and fix whatever newly surfaces (expect the count
to shift, since a version bump can both add and remove diagnostics).

## 7. `qdrant` migration backend is unimplemented

`grimoire/cli/migrate.py` — `--to qdrant` now fails with a clean
`ClickException` instead of silently faking success (fixed in this pass),
but it's still not implemented: no `grimoire/vectorstore/qdrant.py`, no
`qdrant-client` dependency, no `settings.QDRANT_URL`. If Qdrant support is
still wanted as a vector-store backend, this needs a real implementation
(new adapter module, dependency, config, and migration logic) — otherwise
consider removing it from the accepted `--to` choices entirely.

## 8. Pre-existing test failures (not part of this pass)

14 tests fail identically at baseline (`eb1496b`) and after this cleanup —
confirmed via stash/restore diffing, zero regressions introduced. Failure
groups:

- redis-dependent security/rate-limit tests (need a running redis, or
  better test isolation/mocking)
- `torch` / `sentence_transformers` environment-dependent tests
- parser tests
- token-persistence tests

**Action:** separate investigation — likely missing test fixtures/services
in the current dev environment rather than code defects. Worth a
`pytest -k <name> -v` triage session to confirm which are environment gaps
vs. real bugs.
