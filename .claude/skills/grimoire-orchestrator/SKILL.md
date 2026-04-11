---
name: grimoire-orchestrator
description: Orchestrator for Grimoire development tasks. Use this for ANY implementation, bug fix, feature addition, refactor, or testing task in the Grimoire knowledge management codebase. Triggers when working on ingestion, query/RAG, content generation, storage/DB, API, CLI, agents, or anything related to the Grimoire project. Also triggers for re-runs, partial updates, follow-ups, and 'do that again' requests on prior Grimoire work.
---

# Grimoire Development Orchestrator

Routes development tasks to the appropriate specialist agents, coordinates parallel work, and ensures QA coverage.

## Specialist Agents

| Agent | Modules |
|-------|---------|
| `grimoire-ingestion` | parser, chunker, embedder, tagger, dedup, `agents/ingestion.py`, CLI/API ingest |
| `grimoire-query` | hybrid search, reranker, RAG, `agents/query.py`, CLI/API query |
| `grimoire-content` | content_gen agent, generate CLI/API |
| `grimoire-infra` | DB models/migrations, vectorstore, storage adapters, config, cache, coordinator agent, watcher agent |
| `grimoire-api-cli` | FastAPI routers, Pydantic schemas, Click commands, output formatters |
| `grimoire-qa` | Tests, coverage, boundary validation, lint, type check |

## Execution Mode

**Subagent** (default for single-domain tasks) — route directly to one specialist.
**Fan-out subagents** (parallel) — for multi-domain tasks, spawn multiple specialists simultaneously, then collect results.
**QA always follows implementation.**

## Phase 0: Context Check

Before starting, check for prior work:

```bash
ls _workspace/ 2>/dev/null && echo "PRIOR WORKSPACE EXISTS"
```

- `_workspace/` exists + user asks for partial change → **partial re-run**: only re-invoke the relevant specialist
- `_workspace/` exists + new task → move `_workspace/` to `_workspace_prev/`, start fresh
- `_workspace/` missing → **initial run**

## Phase 1: Task Analysis

1. Read the task request carefully.
2. Identify which modules are affected (see table above).
3. Classify the task:
   - **Single-domain**: one specialist handles it entirely
   - **Multi-domain**: multiple specialists work in parallel, then QA validates boundaries
   - **QA/validation only**: send directly to `grimoire-qa`
4. Check if existing tests cover the affected area: `uv run pytest --collect-only -q 2>/dev/null | grep <module_name>`

## Phase 2: Route to Specialists

### Single-domain task

Spawn one specialist subagent with the full task context:

```
Agent(
  description="<task summary>",
  subagent_type="general-purpose",
  model="opus",
  prompt="""
  You are the grimoire-<domain> specialist. Read your agent definition at
  /home/ssund/Code/Grimoire/.claude/agents/grimoire-<domain>.md first.

  ## Task
  <full task description>

  ## Relevant files
  <file paths and line numbers>

  ## Context
  Working directory: /home/ssund/Code/Grimoire
  Run commands with: uv run <command>
  """
)
```

### Multi-domain task (fan-out)

Spawn all relevant specialists in parallel with `run_in_background=true`. Save intermediate results to `_workspace/`:

```
File naming: _workspace/{domain}_{artifact}.md
```

After all specialists complete, run QA.

## Phase 3: QA Validation

After any implementation work, always spawn `grimoire-qa`:

```
Agent(
  description="QA validation for <task>",
  subagent_type="general-purpose",
  model="opus",
  prompt="""
  You are the Grimoire QA agent. Read your definition at
  /home/ssund/Code/Grimoire/.claude/agents/grimoire-qa.md first.

  ## Modules to validate
  <list of modified modules>

  ## Boundary checks needed
  <specific interfaces to verify>

  Working directory: /home/ssund/Code/Grimoire
  """
)
```

## Phase 4: Synthesize Results

Collect all specialist and QA outputs. Report to user:
- What was implemented/changed (with file:line references)
- QA result: PASS/FAIL/PARTIAL
- Any failures or boundary issues found
- Next steps if failures exist

## Error Handling

- Specialist fails → retry once with more specific context; if it fails again, report the failure to the user rather than continuing
- QA fails → report specific failures with file:line, suggest which specialist to re-run
- Domain ambiguity → assign to `grimoire-infra` (broadest scope) or ask user to clarify

## Domain Routing Quick Reference

| Keyword in task | Primary agent |
|-----------------|---------------|
| parse, ingest, chunk, embed, tag, dedup | `grimoire-ingestion` |
| search, query, RAG, rerank, retrieval, ask | `grimoire-query` |
| summary, flashcard, cliff-note, outline, generate | `grimoire-content` |
| DB, migration, schema, Alembic, vector store, storage, adapter, Redis, config, coordinator, watcher | `grimoire-infra` |
| API, endpoint, router, CLI, command, Click, FastAPI, schema | `grimoire-api-cli` |
| test, coverage, lint, type check, validate, regression | `grimoire-qa` |

## Test Scenarios

### Normal flow: Single-domain feature
- User: "Add a `--min-chunk-size` option to the ingest CLI"
- Route: `grimoire-api-cli` (CLI flag) + `grimoire-ingestion` (chunker logic) in parallel
- QA: validate CLI flag wires through to chunker correctly

### Normal flow: Bug fix
- User: "Fix the reranker returning None when top-k < 3"
- Route: `grimoire-query` only
- QA: verify fix with test for k=1, k=2, k=3

### Error flow: QA fails
- QA reports boundary mismatch between chunker output and embedder input
- Escalate to `grimoire-ingestion` with specific shape mismatch details
- Re-run QA after fix
