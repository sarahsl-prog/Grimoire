# Implementation Plan: `docs list` Command & Generate by Category

**Date**: 2026-04-03
**Status**: Planned
**Related Issues**: None yet — create issues when starting work.

---

## Overview

Two related features that improve document discoverability and content generation workflows:

1. **`grimoire docs list`** — List ingested documents with filters (category, title search, date)
2. **`--category` on generate commands** — Generate content from all documents in a category instead of requiring individual doc IDs

---

## Feature 1: `grimoire docs list`

### Usage

```bash
grimoire docs list                                          # all docs
grimoire docs list --category "machine-learning"            # by category name/slug
grimoire docs list --search "quantization"                  # title substring search
grimoire docs list --since 2026-03-01                       # absolute date
grimoire docs list --since 7d                               # relative: days
grimoire docs list --since 2w                               # relative: weeks
grimoire docs list -c cybersecurity --search playbook       # combined filters
grimoire docs list --format json                            # JSON output
```

All filters are combinable (AND logic).

### New File: `grimoire/cli/docs.py`

Create a Click group `docs` with a `list` subcommand. Follow the pattern in `categories.py` and `status.py`.

**Click options:**
- `--category` / `-c` (str, optional) — filter by category name or slug
- `--search` / `-s` (str, optional) — case-insensitive title substring search
- `--since` (str, optional) — date filter (ISO date or relative like `7d`, `2w`, `3m`)
- `--format` (`fmt`, Choice `["text", "json"]`, default `"text"`) — output format

**Query construction:**
- Start with `select(Document)`.
- If `--category`: join `DocumentTag` and `Category`, filter where `func.lower(Category.name) == category.lower()` OR `Category.slug == category.lower()`. Note: `DocumentTag` has composite PK (`document_id`, `category_id`) — join is `Document.id == DocumentTag.c.document_id` and `DocumentTag.c.category_id == Category.id`.
- If `--search`: filter with `Document.title.ilike(f"%{search}%")`.
- If `--since`: parse with `_parse_since()` (see below), filter with `Document.created_at >= parsed_date`.
- Order by `Document.created_at.desc()`.

**`_parse_since(value: str) -> datetime` helper (private, in docs.py):**
- Match against regex `r"^(\d+)([dwm])$"` for relative durations:
  - `d` = days, `w` = weeks (×7 days), `m` = months (��30 days)
  - Subtract `timedelta` from `datetime.utcnow()`
- Otherwise try `datetime.fromisoformat(value)`
- On failure, raise `click.BadParameter` with a clear message

**Text output format:**
```
ID        Title                           Type  Status     Created
────────  ──────────────────────────────  ────  ─────────  ──────────
834b3195  Devpost-AI-Hackathon-Playbook   pdf   completed  2026-04-03
274dd7a2  LLM_Int8                        pdf   completed  2026-04-03
```

Show first 8 chars of ID (enough to copy-paste for other commands). Include total count at the bottom.

**JSON output:** List of objects with full `id`, `title`, `file_type`, `processing_status`, `created_at`, and `categories` (list of category names from tags).

**Lifecycle:** Use `setup_db()` / `teardown_db()` / `get_db_context()` per standard CLI pattern.

### Register in `grimoire/cli/main.py`

Add import and `cli.add_command(docs)` alongside existing command registrations (~line 57-65).

---

## Feature 2: `--category` on Generate Commands

### Usage

```bash
grimoire generate summary --category "machine-learning"
grimoire generate outline --category "cybersecurity"
grimoire generate flashcards --category "Biblical Studies"
grimoire generate cliff-notes --category "development"

# Existing doc-id usage still works
grimoire generate summary -d 834b3195-...
```

`--doc-id` and `--category` are mutually exclusive. At least one is required.

### Modify: `grimoire/cli/generate.py`

**Step 1: Add `_resolve_doc_ids` async helper**

Replace the existing unused `_parse_doc_ids` function (lines 25-29) with:

```python
async def _resolve_doc_ids(
    db: AsyncSession,
    doc_id: tuple[str, ...],
    category: str | None,
) -> list[str]:
```

Logic:
- If both `doc_id` and `category` provided: raise `click.UsageError("Use --doc-id or --category, not both.")`
- If neither provided: raise `click.UsageError("Provide --doc-id or --category.")`
- If `doc_id`: return `list(doc_id)`
- If `category`: query DB joining `Document`, `DocumentTag`, `Category` where category name/slug matches and `Document.processing_status == ProcessingStatus.COMPLETED`. Return list of document IDs. If empty, call `echo_error("No documents found in category '{category}'")` and return empty list.

**Step 2: Modify each subcommand (`summary`, `flashcards`, `cliff_notes`, `outline`)**

For each of the four commands:
1. Change `--doc-id` from `required=True` to `required=False` (keep `multiple=True`)
2. Add `@click.option("--category", type=str, default=None, help="Generate from all docs in this category.")`
3. Add `category: str | None` to function signature
4. Move `async with get_db_context() as db:` to wrap both ID resolution and generation
5. Replace `ids = list(doc_id)` with:
   ```python
   ids = await _resolve_doc_ids(db, doc_id, category)
   if not ids:
       return
   ```

---

## Test Plan

### Modify: `tests/test_cli.py`

**New test class: `TestDocsListCommand`**
- `test_docs_list_all` — no filters, returns all documents
- `test_docs_list_by_category` — `--category "Research"`, verifies DB join/filter
- `test_docs_list_by_search` — `--search "quantization"`, verifies title filter
- `test_docs_list_by_since_relative` — `--since 7d`, verifies date parsing and filter
- `test_docs_list_by_since_absolute` — `--since 2026-03-01`
- `test_docs_list_combined_filters` — `--category X --search Y --since 7d`
- `test_docs_list_json_output` — `--format json`, validates JSON structure
- `test_docs_list_empty` — no matching documents, graceful output
- `test_docs_list_bad_since` — invalid `--since` value, clean error

**New tests in `TestGenerateCommand`:**
- `test_generate_summary_with_category` — `--category "machine-learning"` resolves IDs and generates
- `test_generate_flashcards_with_category` — same for flashcards
- `test_generate_both_flags_error` — both `--doc-id` and `--category` → UsageError
- `test_generate_neither_flag_error` — neither → UsageError
- `test_generate_category_no_docs` — category exists but has no documents → error message

**Update existing tests:**
- `TestCommandRegistration.test_main_help` — add `"docs"` to expected commands list
- `TestCommandRegistration.test_subcommand_help` — add `(["docs", "--help"], "list")`
- `TestCLIEdgeCases.test_generate_no_doc_id` — error message may change from Click's `required` to custom `UsageError`; update assertion

Mock pattern: patch `setup_db`, `teardown_db`, `get_db_context` at `grimoire.cli.docs` / `grimoire.cli.generate` per existing test conventions.

---

## Implementation Order

1. Create `grimoire/cli/docs.py` with the `docs list` command and all filters
2. Register in `grimoire/cli/main.py`
3. Test `docs list` manually and add unit tests
4. Modify `grimoire/cli/generate.py` — add `_resolve_doc_ids` and `--category` to all subcommands
5. Test generate with `--category` manually and add unit tests
6. Update README usage examples

---

## Edge Cases to Handle

- **Category name with spaces**: `--category "Biblical Studies"` — shell quoting handles this, Click receives it as a single string
- **Category not found**: Print `"No category found matching 'X'"` and exit cleanly
- **No documents in category**: Print `"No documents found in category 'X'"` and exit cleanly
- **Slug vs name**: Match on both `Category.name` (case-insensitive) and `Category.slug` (exact) with OR
- **`--since` malformed input**: `click.BadParameter` with example of valid formats
- **Short IDs in output**: 8-char prefix is sufficient for copy-pasting to `--doc-id` (will need to verify the generate/tag commands accept partial IDs, or document that full IDs are needed — consider adding prefix matching)
