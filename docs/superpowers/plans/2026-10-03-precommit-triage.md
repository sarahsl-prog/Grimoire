# `pre-commit run --all-files` — triage (follow-up F2)

**Status:** triage only. Nothing here has been changed; the decisions at the end
come first. Measured on 2026-10-03 against `main`, using the **versions
`.pre-commit-config.yaml` pins** (ruff 0.15.8, black 26.3.1, mypy 1.20.0,
bandit 1.9.4), not the newer ones in a typical dev environment. The counts are
the same with either.

## Bottom line

`pre-commit run --all-files` fails on every one of its four hooks, but the
failures are not one pile of 174 problems. They are three different things:

1. **A hook that cannot agree with the project (mypy).** It runs in an isolated
   environment that does not contain the project's dependencies, so it reports 60
   errors there that do not exist in the real environment, and *contradicts* it
   in places (see below). No amount of code cleanup makes it pass reliably.
2. **A config that does not match the repo (ruff/bandit on tests and a few
   intentional patterns).** About 87 of the 174 ruff findings (74 in tests, 13
   intentional `E402`) are noise and are one config change each.
3. **A real but modest backlog** (about 58 findings, once `UP042` is decided), plus **two security-relevant
   things hiding behind the noise** that deserve a decision of their own.

Cleaning it up is a few small PRs, not a big-bang rewrite, and nothing needs a
risky refactor. The one place I would not auto-fix is `UP042` (29 findings),
because the fix changes behaviour.

## What each hook reports

| Hook | Findings | What they are |
|---|---|---|
| ruff | 174 (99 in `tests/`, 74 in `grimoire/`, 1 in `rag_pipeline.py`) | see below |
| black | 5 files | formatting only: `grimoire/cli/status.py` and four under `tests/deploy/`. Mechanical. |
| bandit (`grimoire/` only, as the hook scopes it) | 8 (3 medium, 5 low, 0 high) | see "Security" below |
| mypy | 60 in the hook's environment; about 10 in a dev environment, all environment artifacts | see below |

### ruff, by what to do about it

| Rule | Count | Where | Verdict |
|---|---|---|---|
| `S108` hardcoded `/tmp` | 52 | tests | **Noise.** Tests legitimately use fixed temp paths. The config already ignores `S101`/`S105` for tests; extend it. (Some could use `tmp_path`, which is also F4.) |
| `S106` hardcoded password argument | 15 | tests | **Noise.** Fake credentials in test fixtures. |
| `S603`/`S607`/`S110`/`S103` | 7 | tests | Noise for test code. |
| `UP042` use `StrEnum` | 29 | `grimoire/` (14 in `db/models.py`, 6 in `config/settings.py`) | **Do not auto-fix.** `class X(str, Enum)` and `StrEnum` print differently (`str(X.A)` is `X.A` for the first, the value for the second), and these enums feed SQLAlchemy and settings. Either ignore the rule with that reason, or audit every `str()`/f-string use first. |
| `E402` import not at top | 13 | `grimoire/cli/main.py` (12), `storage/local.py` (1) | **Intentional.** The CLI registers subcommands after defining the group; this is `FUTURE_TODO.md` item 2. A `per-file-ignores` entry, not 12 inline `noqa`s. |
| `C901` too complex (> 12) | 7 | parsers, tagger, chunker, corpus | **Real but not a lint fix.** These are the repo's genuinely complex functions (complexity 13 to 20). Options: raise the threshold, mark them with a reason, or refactor each. I would not refactor to satisfy a linter. |
| `SIM102`/`SIM103`/`SIM108`/`SIM101`/`SIM118`/`SIM105`/`SIM117` | 21 | both | Real, trivial, safe to fix by hand (I would not use the "unsafe" auto-fix). |
| `B007` unused loop variable, `F841` unused local, `B011`, `B905`, `C401`, `W293` | 15 | tests | Real, trivial. `F841` may hide a test that discards a result it meant to assert on; worth a look as each is fixed. |
| `N806`/`N811`/`N802` naming | 6 | `db/models.py` (aliases `SQLEnum`, `BaseJSON`), `vectorstore/chromadb.py`, tests | Mostly deliberate aliases; fix or `noqa` with a reason. |
| `S105` "hardcoded password" | 3 | `storage/gdrive.py`, `storage/onedrive.py` | **False positives**: they are OAuth token *URLs* and a token *type* string. `noqa` with a reason. |
| `UP047`, `UP007` | 3 | `cli/query.py`, `mcp/mlflow_logging.py`, `retriever.py` | Small modernisations. |

### mypy: why the hook cannot be trusted as configured

The hook builds a throwaway environment containing only the packages listed under
`additional_dependencies`. The project has many more (slowapi, textual, httpx,
mlflow, docling, ...). Consequences, measured:

- It reports **60 errors** that the real environment does not have: 26 "class
  cannot subclass X (has type Any)", 13 "untyped decorator", 12 unused ignores, 9
  "returning Any". Each one is a package that is simply not installed there.
- It **contradicts the code**. Several `# type: ignore[...]` comments exist
  *because* a package is installed (they are needed with `docling` or `slowapi`
  present and "unused" without). With `warn_unused_ignores = true`, the hook
  flags them as errors, while a real environment flags the opposite if they are
  removed. For example `grimoire/api/main.py` (the `limiter.exempt` ignore) and
  `grimoire/core/parser.py` (six ignores around the optional Docling import).
- In a plain dev environment, what remains (about 10) is environmental: missing
  `types-PyYAML` stubs and the same optional-dependency ignores.

The standard fix is to run mypy as a **local hook in the project's own
environment** (`language: system`, `pass_filenames: false`, `entry: uv run mypy
grimoire/`) instead of a mirrored one with a hand-copied dependency list. I could
not verify a fully clean `mypy grimoire/` here, because docling (and its torch
dependency) is not installable in this sandbox; that needs one run on a real dev
machine.

### Security: two findings worth a decision of their own

These are not lint noise.

1. **`grimoire/api/routes/ingest.py:36`** (ruff `S108`): the ingest-by-path
   allowlist is `[Path("/tmp").resolve(), Path("/home/sunds").resolve()]`. That
   is every file under `/tmp` and under one developer's home directory, hardcoded,
   for any API key allowed to call the endpoint. It looks like a development
   leftover. I have not changed it: it is a security-sensitive behaviour change
   and needs your call (configurable setting? remove the home directory?).
2. **`grimoire/cli/mcp.py:27` and `config/settings.py:731`** (bandit `B104`):
   `grimoire mcp --sse` defaults to `--host 0.0.0.0`, binding every interface.
   That is deliberate for the container deployment, but on a bare-metal laptop it
   exposes the MCP server to the network by default. Safer default is
   `127.0.0.1` with the compose file passing `0.0.0.0` explicitly. Again a
   behaviour change, so your call.

The other six bandit findings are low-risk: `subprocess` use for `$EDITOR` in
`cli/config.py` (an intentional call to a user-chosen editor), a deliberate
`except: pass` in `mcp/tools.py` (already `noqa`d for ruff), and two `B105` false
positives on OAuth token URLs.

## Proposed order (each its own PR, none large)

1. **Make the hooks agree with the project.** Replace the mirrored mypy hook with
   a local one; add `per-file-ignores` for the test-only noise and for the
   intentional `E402`; run black over the 5 files. After this, ruff drops from 174
   to about 87, or about 58 if `UP042` is also ignored.
2. **Fix the real backlog by hand:** the 21 `SIM*` simplifications, the 15 test
   findings (checking each unused-variable one), the naming ones, and `noqa` with
   a reason on the confirmed false positives (`S105`, `S603`, `B110`).
3. **Decide the three policy items below**, then apply them.
4. **Document it:** one line in `CLAUDE.md` Code Quality saying `pre-commit run
   --all-files` is expected to be clean, and how to run each tool.

There is no CI configuration in the repository, so nothing enforces the gate
except people running it. Clean-first matters: turning on a failing gate only
teaches everyone to skip it.

## Decisions needed

1. **Is `pre-commit` meant to be a gate** (clean on `--all-files`, a hook people
   actually run), or should it be removed or downgraded to advisory? I recommend
   making it clean and keeping it.
2. **`UP042` (29 enum findings):** ignore the rule with the reason above (my
   recommendation: the audit buys nothing), or audit and convert to `StrEnum`.
3. **`C901` (7 complex functions):** raise the threshold, mark each with a reason,
   or refactor. My recommendation: mark them with a reason and track the
   refactors separately.
4. **The two security items:** what should `ingest` allow, and what should
   `grimoire mcp --sse` bind to by default?
5. **mypy hook:** local hook in the project environment (my recommendation), or
   keep the mirrored one and fix its dependency list (fragile: it will drift
   again).
