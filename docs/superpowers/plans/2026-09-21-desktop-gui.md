# Grimoire Desktop GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a PySide6 desktop client with four tabs — Search/Ask, Recent ingests, drag-and-drop ingest, CLI runner — plus the multipart upload endpoint the drop zone needs.

**Architecture:** The GUI is a thin synchronous HTTP client over the existing REST API. It imports no pipeline code, so the GUI process never loads torch, Docling, or ChromaDB. Every network call runs on a `QThreadPool` worker; the CLI tab uses `QProcess`. PySide6 lives behind an optional-dependency group so the server image and CI stay Qt-free.

**Tech Stack:** Python 3.13, PySide6 6.7+, httpx (sync), FastAPI, Pydantic v2, pytest + pytest-qt, loguru.

**Spec:** [`docs/superpowers/specs/2026-09-21-desktop-gui-design.md`](../specs/2026-09-21-desktop-gui-design.md)

## Global Constraints

- **No asyncio in the GUI.** `httpx.Client`, never `AsyncClient`. Qt owns the event loop.
- **Nothing blocking on the GUI thread.** Every API call goes through `ApiWorker` on a `QThreadPool`; Qt objects are touched only in signal slots.
- **Raw exceptions never reach a widget.** `GrimoireClient` converts every failure to a `GuiError` with human-readable text; the original goes to `loguru`.
- **`mypy --strict` must pass** over `grimoire/` including `grimoire/gui/`. A narrow, commented `# type: ignore[code]` is acceptable where a PySide6 stub is unusable; a module-level exclusion is not.
- **Ruff selects** `E,W,F,I,N,UP,B,C4,SIM,S,C90`; `E501` and `B008` are ignored globally, `S101`/`S105` in `tests/**`. Max mccabe complexity 12. Line length 88, formatted by black.
- **Timeouts:** connect 5s everywhere; read 300s for `/query/ask` and upload; read 30s for search and document listing.
- **API defaults:** base URL `http://localhost:8001` (`APIConfig.port` default is 8001, not 8000). Auth header is `X-API-Key`.
- **Upload cap default:** 100 MB (`100 * 1024 * 1024` bytes). Staging files are **kept**, never deleted after ingest.
- **Environment variables:** `GRIMOIRE_API_URL`, `GRIMOIRE_API_KEY` for the GUI. An API key entered in the GUI is held in memory only — never written to disk.
- **Tests:** `asyncio_mode = "auto"` is already set; do not add `@pytest.mark.asyncio`. Qt tests require `QT_QPA_PLATFORM=offscreen`.
- **Commits:** imperative, one line, no trailing period. One task per commit — never batch.

---

## File Structure

**Server side (Tasks 1–3):**

| File | Responsibility |
|---|---|
| `grimoire/config/settings.py` | Add `upload_dir` + `max_upload_bytes` to `APIConfig` |
| `grimoire/api/routes/ingest.py` | Add `POST /upload`: extension check, streamed write, cap enforcement |
| `docker-compose.yml` | `app_uploads` named volume + `GRIMOIRE_API__UPLOAD_DIR` |
| `README.md`, `docs/deploy/docker.md` | Document the endpoint and the volume |

**GUI side (Tasks 4–11):**

| File | Responsibility |
|---|---|
| `grimoire/gui/config.py` | `GuiConfig` — env parsing, timeouts, supported extensions. No Qt, no I/O. |
| `grimoire/gui/errors.py` | `GuiError` hierarchy. No Qt, no httpx. |
| `grimoire/gui/client.py` | `GrimoireClient` — one method per endpoint, maps failures to `GuiError`. No Qt. |
| `grimoire/gui/workers.py` | `ApiWorker`/`WorkerSignals` — run a callable off the GUI thread. |
| `grimoire/gui/app.py` | `MainWindow`, tab assembly, status bar, shutdown. |
| `grimoire/gui/__main__.py` | Entry point: build config, client, window, run. |
| `grimoire/gui/widgets/connection_bar.py` | Base URL display, key state, session key entry. |
| `grimoire/gui/widgets/citation_card.py` | One source chunk. Shared by both query modes. |
| `grimoire/gui/widgets/search_tab.py` | Ask/Search toggle, answer panel, cards. |
| `grimoire/gui/widgets/recent_tab.py` | Ten-row document table. |
| `grimoire/gui/widgets/ingest_tab.py` | Drop zone, queue, progress. |
| `grimoire/gui/widgets/cli_tab.py` | `QProcess` runner, allowlist, output view. |

The split is by responsibility: `config`/`errors`/`client` are Qt-free and testable without a display; every file under `widgets/` owns exactly one tab or one reusable visual unit.

---

## Task 1: Upload configuration fields

**Files:**
- Modify: `grimoire/config/settings.py:714-748` (`APIConfig`)
- Test: `tests/test_config_validation.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `APIConfig.upload_dir: Path` (default `Path("uploads")`), `APIConfig.max_upload_bytes: int` (default `104857600`). Reached as `get_settings().api.upload_dir` / `.max_upload_bytes`.

**Context:** `APIConfig` sets `model_config = ConfigDict(extra="forbid")`, so these fields must be declared on the model — they cannot be smuggled in as extras. `Path` is already imported at `grimoire/config/settings.py:17`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_validation.py`:

```python
class TestAPIConfigUploads:
    """Upload staging configuration on APIConfig."""

    def test_upload_defaults(self) -> None:
        from grimoire.config.settings import APIConfig

        cfg = APIConfig()
        assert cfg.upload_dir == Path("uploads")
        assert cfg.max_upload_bytes == 100 * 1024 * 1024

    def test_upload_dir_accepts_absolute_path(self) -> None:
        from grimoire.config.settings import APIConfig

        cfg = APIConfig(upload_dir=Path("/app/uploads"))
        assert cfg.upload_dir == Path("/app/uploads")

    def test_max_upload_bytes_rejects_zero(self) -> None:
        from pydantic import ValidationError

        from grimoire.config.settings import APIConfig

        with pytest.raises(ValidationError):
            APIConfig(max_upload_bytes=0)
```

Add `from pathlib import Path` and `import pytest` to that file's imports if they are not already present.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_config_validation.py::TestAPIConfigUploads -v
```

Expected: FAIL — `ValidationError: Extra inputs are not permitted` for `upload_dir`, and `AttributeError`/assertion failure on the defaults test.

- [ ] **Step 3: Add the fields**

In `grimoire/config/settings.py`, inside `class APIConfig`, after the `secret_key` field and before the `_reject_placeholder_secret` validator:

```python
    upload_dir: Path = Field(
        default=Path("uploads"),
        description="Staging directory for files uploaded through /ingest/upload",
    )
    max_upload_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        description="Maximum accepted upload size in bytes",
    )
```

Extend the class docstring's `Attributes:` block with:

```
        upload_dir: Staging directory for uploaded files. Uploads are kept,
            not deleted after ingest, because Document.source_path points at
            them.
        max_upload_bytes: Hard cap enforced while streaming an upload.
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_config_validation.py::TestAPIConfigUploads -v
uv run pytest tests/test_config_validation.py -v
```

Expected: PASS, and no regression in the rest of the file.

- [ ] **Step 5: Type-check and lint**

```bash
uv run mypy grimoire/config/settings.py
uv run ruff check grimoire/config/settings.py
uv run black --check grimoire/config/settings.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add grimoire/config/settings.py tests/test_config_validation.py
git commit -m "$(cat <<'EOF'
Add upload staging directory and size cap to APIConfig

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `POST /api/v1/ingest/upload`

**Files:**
- Modify: `grimoire/api/routes/ingest.py`
- Modify: `pyproject.toml` (add explicit `python-multipart` dependency)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `APIConfig.upload_dir`, `APIConfig.max_upload_bytes` (Task 1).
- Produces: `POST /api/v1/ingest/upload`, multipart form with fields `file` (the upload) and `auto_tag` (`"true"`/`"false"`, default true). Returns the existing `IngestResultResponse`. Status codes: 200 success, 401 no/bad key, 413 over cap, 415 unsupported extension, 500 write failure.

**Context:** The existing `/ingest/file` validates a client-supplied path against `_ALLOWED_ROOTS = [/tmp, /home/sunds]` (`grimoire/api/routes/ingest.py:25`), which a containerized API cannot satisfy for a host file. This endpoint has no traversal surface at all: the server picks the path and the client's filename contributes only a sanitized basename.

`python-multipart` is currently present only transitively (via `mcp`). FastAPI's `UploadFile` requires it directly, so it becomes a declared dependency.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`, inside the class that holds the other ingest tests (the one whose methods are `test_ingest_file` / `test_ingest_directory`):

```python
    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_upload_ingests_file(self, mock_get_agent, client, tmp_path, monkeypatch):
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "file_path": "staged",
            "document_id": "doc-1",
            "status": "completed",
            "chunks_created": 5,
            "vectors_stored": 5,
            "tags_applied": 2,
            "error_message": None,
            "duration_ms": 100,
        }
        mock_agent.ingest_file = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("notes.md", b"# hello", "text/markdown")},
            data={"auto_tag": "false"},
        )

        assert resp.status_code == 200
        assert resp.json()["document_id"] == "doc-1"
        staged = list(tmp_path.iterdir())
        assert len(staged) == 1
        assert staged[0].name.endswith("_notes.md")
        assert staged[0].read_bytes() == b"# hello"
        mock_agent.ingest_file.assert_awaited_once()
        assert mock_agent.ingest_file.await_args.kwargs["auto_tag"] is False

    def test_upload_rejects_unsupported_extension(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
        )

        assert resp.status_code == 415
        assert ".exe" in resp.json()["detail"]
        assert list(tmp_path.iterdir()) == []

    def test_upload_rejects_oversized_file(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)
        monkeypatch.setattr(f"{_ROUTES_INGEST}._max_upload_bytes", lambda: 8)

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("big.txt", b"x" * 4096, "text/plain")},
        )

        assert resp.status_code == 413
        assert list(tmp_path.iterdir()) == [], "partial file must be removed"

    @patch(f"{_ROUTES_INGEST}.get_ingestion_agent")
    def test_upload_sanitizes_traversal_filename(
        self, mock_get_agent, client, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)
        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "file_path": "staged",
            "document_id": "doc-2",
            "status": "completed",
            "chunks_created": 1,
            "vectors_stored": 1,
            "tags_applied": 0,
            "error_message": None,
            "duration_ms": 10,
        }
        mock_agent.ingest_file = AsyncMock(return_value=mock_result)
        mock_get_agent.return_value = mock_agent

        resp = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("../../etc/passwd.txt", b"root", "text/plain")},
        )

        assert resp.status_code == 200
        staged = list(tmp_path.iterdir())
        assert len(staged) == 1
        assert staged[0].parent == tmp_path
        assert staged[0].name.endswith("_passwd.txt")
        assert ".." not in staged[0].name
```

Add this helper near the top of `tests/test_api.py`, beside `_make_test_api_key`:

```python
def _upload_dir_patch_target() -> str:
    """Dotted path of the staging-dir helper, patched to a tmp_path in tests."""
    return f"{_ROUTES_INGEST}._staging_dir"
```

Also add an unauthenticated case. The shared `client` fixture overrides
`get_api_key`, so use a fixture-free app for this one:

```python
    def test_upload_requires_api_key(self, app, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from grimoire.api.dependencies import get_db_session

        monkeypatch.setattr(_upload_dir_patch_target(), lambda: tmp_path)

        async def override_db():
            yield AsyncMock()

        app.dependency_overrides[get_db_session] = override_db
        try:
            with TestClient(app, raise_server_exceptions=False) as unauth:
                resp = unauth.post(
                    "/api/v1/ingest/upload",
                    files={"file": ("notes.md", b"# hello", "text/markdown")},
                )
            assert resp.status_code == 401
        finally:
            app.dependency_overrides.clear()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_api.py -k upload -v
```

Expected: FAIL — 404 on `/api/v1/ingest/upload`, and `AttributeError` on the patch targets, because neither the route nor the helpers exist.

- [ ] **Step 3: Add the dependency**

In `pyproject.toml`, in `[project] dependencies`, after `"python-magic>=0.4",`:

```toml
    "python-multipart>=0.0.9",
```

Then:

```bash
uv lock && uv sync --extra dev
```

- [ ] **Step 4: Implement the endpoint**

In `grimoire/api/routes/ingest.py`, extend the imports:

```python
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from loguru import logger
```

Add below `_MAX_PATH_LEN`:

```python
# Read the body a megabyte at a time.  Streaming rather than awaiting the
# whole upload keeps a large file off the heap: one chunk is resident at a
# time regardless of the file's size.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


class _UploadTooLargeError(Exception):
    """Internal signal that a streamed upload passed the configured cap."""


def _staging_dir() -> Path:
    """Return the upload staging directory, creating it if absent.

    Patched in tests.  Uploads are kept here permanently: ``ingest_file``
    records the path it is given as ``Document.source_path``, so deleting the
    staged file would orphan the row.
    """
    from grimoire.config.settings import get_settings

    upload_dir = Path(get_settings().api.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def _max_upload_bytes() -> int:
    """Return the configured upload cap.  Patched in tests."""
    from grimoire.config.settings import get_settings

    return int(get_settings().api.max_upload_bytes)


def _supported_extensions() -> set[str]:
    """Return the parser's accepted extensions.

    Imported lazily: ``grimoire.core.parser`` imports Docling at module
    scope, which is far too heavy to pay for at route-module import time.
    """
    from grimoire.core.parser import DocumentParser

    return DocumentParser.SUPPORTED_EXTENSIONS


async def _stream_upload_to_disk(
    upload: UploadFile, destination: Path, max_bytes: int
) -> int:
    """Write an upload to ``destination`` in chunks, enforcing ``max_bytes``.

    Returns the number of bytes written.  On any failure the partial file is
    removed before the HTTPException propagates, so a rejected upload never
    leaves debris in the staging directory.
    """
    written = 0
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise _UploadTooLargeError
                handle.write(chunk)
    except _UploadTooLargeError:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the maximum upload size of {max_bytes} bytes",
        ) from None
    except OSError as exc:
        destination.unlink(missing_ok=True)
        logger.error(f"Failed to write upload to {destination}: {exc}")
        raise HTTPException(
            status_code=500, detail="Could not store the uploaded file"
        ) from exc
    return written
```

Add the route after `ingest_file`:

```python
@router.post("/upload", response_model=IngestResultResponse)
async def ingest_upload(
    request: Request,
    file: UploadFile = File(...),
    auto_tag: bool = Form(default=True),
    api_key: ApiKey = Depends(get_api_key),
    db: AsyncSession = Depends(get_db_session),
) -> IngestResultResponse:
    """Ingest a file uploaded in the request body.

    Unlike ``/ingest/file``, this endpoint never trusts a client-supplied
    path.  The server chooses the staging location and the client's filename
    contributes only a sanitized basename, so there is no traversal surface
    to validate.
    """
    original_name = Path(file.filename or "upload").name
    extension = Path(original_name).suffix.lower()
    if extension not in _supported_extensions():
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {extension or '(no extension)'}",
        )

    destination = _staging_dir() / f"{uuid4().hex}_{original_name}"
    written = await _stream_upload_to_disk(file, destination, _max_upload_bytes())
    logger.info(f"Staged upload {original_name} ({written} bytes) at {destination}")

    agent = get_ingestion_agent()
    result = await agent.ingest_file(db, str(destination), auto_tag=auto_tag)
    return IngestResultResponse(**result.model_dump())
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_api.py -k upload -v
uv run pytest tests/test_api.py -v
```

Expected: all upload tests PASS, no regressions in the file.

- [ ] **Step 6: Verify the retention assumption before moving on**

This is the one decision in the spec that is expensive to reverse. Confirm that nothing copies the staged file elsewhere, which would make keeping it redundant:

```bash
grep -n "source_path" grimoire/agents/ingestion.py
grep -rn "copy\|shutil" grimoire/storage/local.py
```

Expected: `ingest_file` stores the given path as `Document.source_path`, and the local storage adapter reads in place rather than copying — so the staged file must persist. **If this turns out to be false** (a copy is made), stop and report it: retention becomes a choice rather than a requirement, and the volume in Task 3 may be sizable dead weight.

- [ ] **Step 7: Type-check and lint**

```bash
uv run mypy grimoire/api/routes/ingest.py
uv run ruff check grimoire/api/routes/ingest.py tests/test_api.py
uv run black --check grimoire/api/routes/ingest.py tests/test_api.py
```

Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add grimoire/api/routes/ingest.py tests/test_api.py pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
Add multipart upload endpoint for host-side ingestion

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Persist uploads across container recreation

**Files:**
- Modify: `docker-compose.yml:32-36` (the `x-grimoire-app` volumes list), `docker-compose.yml:5-19` (the `x-grimoire-env` block), `docker-compose.yml:253-268` (the `volumes:` section)
- Modify: `docs/deploy/docker.md` (`## Volumes and backup` table at line 135, `## Configuration` at line 54)
- Modify: `README.md` (the API examples block, near line 278)

**Interfaces:**
- Consumes: `GRIMOIRE_API__UPLOAD_DIR` is read by `APIConfig.upload_dir` (Task 1) through the existing nested-env convention.
- Produces: uploaded documents survive `docker compose down`.

**Context:** The staging directory holds the **only** copy of every uploaded document. Without a named volume it lives in the container's writable layer and disappears on recreation — which is data loss, not an inconvenience. This ships in the same change as the docs so no deployment can pick up the endpoint without the volume.

- [ ] **Step 1: Add the volume and the environment variable**

In `docker-compose.yml`, in `x-grimoire-env`, after the `GRIMOIRE_CACHE__PATH` line:

```yaml
  GRIMOIRE_API__UPLOAD_DIR: /app/uploads
```

In `x-grimoire-app`, in its `volumes:` list, after `- app_cache:/app/cache`:

```yaml
    - app_uploads:/app/uploads
```

In the top-level `volumes:` section, after the `app_cache` entry:

```yaml
  app_uploads:
    driver: local
```

- [ ] **Step 2: Verify the compose file still parses and the volume is wired**

```bash
docker compose config --quiet && echo "compose OK"
docker compose config | grep -A2 "app_uploads"
docker compose config | grep "GRIMOIRE_API__UPLOAD_DIR"
```

Expected: `compose OK`, the volume appears under both the service mounts and the top-level volumes, and the env var resolves to `/app/uploads`.

- [ ] **Step 3: Document the volume**

In `docs/deploy/docker.md`, add a row to the `## Volumes and backup` table, after the `app_cache` row:

```markdown
| `app_uploads` | Files uploaded through the GUI or `POST /ingest/upload` | **Yes** — the only copy of every uploaded document |
```

Directly under that table, add:

```markdown
`app_uploads` is the one application volume that is not rebuildable.
`POST /api/v1/ingest/upload` stages each uploaded file there and records that
path as the document's `source_path`, so the staged file is the original —
deleting the volume orphans every row that came in through the GUI.
```

In `## Configuration`, add the two new settings to the environment table in the
same style as its neighbours:

```markdown
| `GRIMOIRE_API__UPLOAD_DIR` | `/app/uploads` in containers, `uploads` bare-metal | Where `POST /ingest/upload` stages files |
| `GRIMOIRE_API__MAX_UPLOAD_BYTES` | `104857600` (100 MB) | Upload size cap; larger uploads get a 413 |
```

Match the existing table's column count — inspect it first and adjust if it
has a different shape.

- [ ] **Step 4: Document the endpoint**

In `README.md`, in the API examples code block, after the "Ingest a directory" example:

```bash
# Upload and ingest a file directly (no shared filesystem needed)
curl -X POST http://localhost:8001/api/v1/ingest/upload \
  -H "X-API-Key: $GRIMOIRE_API_KEY" \
  -F "file=@/path/to/document.pdf" \
  -F "auto_tag=true"
```

- [ ] **Step 5: Verify the docs render and nothing else drifted**

```bash
git diff --stat
uv run pytest tests/deploy -v
```

Expected: only the four intended files changed; the deploy tests (which parse the compose file) still pass.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml docs/deploy/docker.md README.md
git commit -m "$(cat <<'EOF'
Persist uploaded files in a named volume and document the upload endpoint

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: GUI package skeleton and configuration

**Files:**
- Create: `grimoire/gui/__init__.py`, `grimoire/gui/config.py`
- Modify: `pyproject.toml`
- Test: `tests/test_gui_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `GuiConfig` — frozen dataclass with `base_url: str`, `api_key: str | None`, `connect_timeout: float = 5.0`, `read_timeout: float = 30.0`, `long_read_timeout: float = 300.0`, `max_upload_bytes: int = 104857600`
  - `GuiConfig.from_env(env: Mapping[str, str] | None = None) -> GuiConfig`
  - `GuiConfig.with_api_key(api_key: str) -> GuiConfig`
  - `GuiConfig.is_configured: bool` (property)
  - `SUPPORTED_EXTENSIONS: frozenset[str]`
  - `DEFAULT_BASE_URL: str`

**Context:** `SUPPORTED_EXTENSIONS` is duplicated here rather than imported from `grimoire.core.parser`, because that module imports Docling at import time (`grimoire/core/parser.py:19-27`) and the GUI must start without paying for it. A test asserts the two sets are identical, so drift becomes a test failure rather than a silent divergence.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gui_config.py`:

```python
"""Tests for GUI configuration parsing.

These tests import no Qt: GuiConfig is deliberately Qt-free so it can be
tested without a display server.
"""

from __future__ import annotations

from grimoire.gui.config import DEFAULT_BASE_URL, SUPPORTED_EXTENSIONS, GuiConfig


class TestGuiConfigFromEnv:
    def test_defaults_when_env_empty(self) -> None:
        cfg = GuiConfig.from_env({})
        assert cfg.base_url == DEFAULT_BASE_URL
        assert cfg.base_url == "http://localhost:8001"
        assert cfg.api_key is None
        assert cfg.is_configured is False

    def test_reads_both_variables(self) -> None:
        cfg = GuiConfig.from_env(
            {"GRIMOIRE_API_URL": "http://box:9000", "GRIMOIRE_API_KEY": "grim_agt_x"}
        )
        assert cfg.base_url == "http://box:9000"
        assert cfg.api_key == "grim_agt_x"
        assert cfg.is_configured is True

    def test_strips_trailing_slash_from_base_url(self) -> None:
        cfg = GuiConfig.from_env({"GRIMOIRE_API_URL": "http://box:9000/"})
        assert cfg.base_url == "http://box:9000"

    def test_blank_key_is_treated_as_absent(self) -> None:
        cfg = GuiConfig.from_env({"GRIMOIRE_API_KEY": "   "})
        assert cfg.api_key is None
        assert cfg.is_configured is False


class TestGuiConfigWithApiKey:
    def test_returns_new_instance_with_key(self) -> None:
        cfg = GuiConfig.from_env({})
        updated = cfg.with_api_key("grim_dvl_y")
        assert updated.api_key == "grim_dvl_y"
        assert updated.base_url == cfg.base_url
        assert cfg.api_key is None, "original config must be unchanged"


class TestSupportedExtensions:
    def test_matches_the_parser(self) -> None:
        # The GUI duplicates this set to avoid importing Docling at startup.
        # If the parser gains a format, this test is what catches the drift.
        from grimoire.core.parser import DocumentParser

        assert SUPPORTED_EXTENSIONS == frozenset(DocumentParser.SUPPORTED_EXTENSIONS)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_gui_config.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'grimoire.gui'`.

- [ ] **Step 3: Declare the optional dependency group**

In `pyproject.toml`, add to `[project.optional-dependencies]`, after the `mlflow` group:

```toml
gui = [
    "PySide6>=6.7",
]
```

Add to the `dev` group list:

```toml
    "pytest-qt>=4.4",
```

Add to `[project.scripts]`:

```toml
grimoire-gui = "grimoire.gui.__main__:main"
```

Install:

```bash
uv lock && uv sync --extra dev --extra gui
```

- [ ] **Step 4: Create the package and config module**

`grimoire/gui/__init__.py`:

```python
"""PySide6 desktop client for Grimoire.

The GUI is a thin HTTP client over the REST API.  Nothing in this package
imports the ingestion or query pipeline, so the GUI process never loads
torch, Docling, or ChromaDB.
"""
```

`grimoire/gui/config.py`:

```python
"""Configuration for the Grimoire desktop client.

Deliberately free of Qt and of any Grimoire pipeline import, so it can be
constructed and tested without a display server or a heavyweight dependency
tree.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace

DEFAULT_BASE_URL = "http://localhost:8001"

ENV_BASE_URL = "GRIMOIRE_API_URL"
ENV_API_KEY = "GRIMOIRE_API_KEY"

# Mirrors DocumentParser.SUPPORTED_EXTENSIONS.  Duplicated rather than
# imported because grimoire.core.parser imports Docling at module scope, and
# the GUI must not pay that cost to filter a drag-and-drop.  tests/
# test_gui_config.py asserts the two stay identical.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".docx",
        ".doc",
        ".pptx",
        ".ppt",
        ".xlsx",
        ".xls",
        ".html",
        ".htm",
        ".md",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
        ".tiff",
        ".tif",
        ".gif",
        ".bmp",
        ".webp",
        ".json",
        ".yaml",
        ".yml",
    }
)


@dataclass(frozen=True)
class GuiConfig:
    """Everything the client needs to talk to a Grimoire API.

    Attributes:
        base_url: API root, without a trailing slash.
        api_key: Value sent as the X-API-Key header, or None when unset.
        connect_timeout: Seconds to wait for a connection.
        read_timeout: Seconds to wait for a fast endpoint's response.
        long_read_timeout: Seconds to wait for /query/ask and uploads, which
            are bounded by LLM generation and document parsing rather than
            by the network.
        max_upload_bytes: Client-side cap, mirroring the server's default so
            an oversized file is rejected before it is sent.
    """

    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    long_read_timeout: float = 300.0
    max_upload_bytes: int = 100 * 1024 * 1024

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GuiConfig:
        """Build a config from the environment.

        Args:
            env: Mapping to read instead of os.environ.  Tests pass an
                explicit dict rather than mutating the process environment.

        Returns:
            A GuiConfig.  Missing values fall back to the defaults; a blank
            or whitespace-only key counts as absent.
        """
        source = os.environ if env is None else env
        raw_url = source.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL
        raw_key = source.get(ENV_API_KEY, "").strip()
        return cls(base_url=raw_url.rstrip("/"), api_key=raw_key or None)

    def with_api_key(self, api_key: str) -> GuiConfig:
        """Return a copy carrying a different key.

        The GUI holds a pasted key in memory only; nothing here writes it to
        disk.
        """
        cleaned = api_key.strip()
        return replace(self, api_key=cleaned or None)

    @property
    def is_configured(self) -> bool:
        """Whether the client has a key to authenticate with."""
        return bool(self.api_key)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_gui_config.py -v
```

Expected: PASS, all seven tests.

- [ ] **Step 6: Type-check and lint**

```bash
uv run mypy grimoire/gui/
uv run ruff check grimoire/gui/ tests/test_gui_config.py
uv run black --check grimoire/gui/ tests/test_gui_config.py
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add grimoire/gui/ tests/test_gui_config.py pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
Add GUI package skeleton with environment-driven configuration

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: API client and error mapping

**Files:**
- Create: `grimoire/gui/errors.py`, `grimoire/gui/client.py`
- Test: `tests/test_gui_client.py`

**Interfaces:**
- Consumes: `GuiConfig`, `SUPPORTED_EXTENSIONS` (Task 4); `POST /ingest/upload` (Task 2).
- Produces:
  - `GuiError(message: str, *, retry_after: int | None = None)` with attribute `.message`
  - Subclasses: `ConnectionFailed`, `TimedOut`, `AuthFailed`, `RequestRejected`, `RateLimited`, `ServerError`, `MalformedResponse`
  - `GrimoireClient(config: GuiConfig)` with methods:
    - `ask(query: str, *, top_k: int = 5, use_cache: bool = True) -> QueryResponse`
    - `search(query: str, *, top_k: int = 10) -> SearchResponse`
    - `recent_documents(*, limit: int = 10) -> DocumentListResponse`
    - `upload(path: Path, *, auto_tag: bool = True) -> IngestResultResponse`
    - `health() -> bool`
    - `close() -> None`
  - `GrimoireClient.config: GuiConfig` (read-only attribute)

**Context:** Responses are parsed with the server's own models from `grimoire.api.schemas`, which imports only Pydantic. One source of truth for the wire format; a server-side schema change becomes a type error here rather than a runtime surprise.

Every public method raises only `GuiError` subclasses. That is the contract the widgets rely on — they never see `httpx` or `ValidationError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gui_client.py`:

```python
"""Tests for the GUI's HTTP client and its error mapping.

No Qt here either — GrimoireClient is plain httpx, so these run without a
display server.
"""

from __future__ import annotations

import httpx
import pytest

from grimoire.gui.client import GrimoireClient
from grimoire.gui.config import GuiConfig
from grimoire.gui.errors import (
    AuthFailed,
    ConnectionFailed,
    MalformedResponse,
    RateLimited,
    RequestRejected,
    ServerError,
    TimedOut,
)

BASE = "http://testapi:8001"


@pytest.fixture
def config() -> GuiConfig:
    return GuiConfig(base_url=BASE, api_key="grim_agt_test")


@pytest.fixture
def client(config: GuiConfig):
    c = GrimoireClient(config)
    yield c
    c.close()


class TestAsk:
    def test_parses_answer_and_citations(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/query/ask",
            json={
                "query": "what is a sigma rule",
                "answer": "A detection rule format.",
                "citations": [
                    {
                        "document_id": "doc-1",
                        "document_title": "Sigma primer",
                        "chunk_id": "chunk-1",
                        "chunk_index": 0,
                        "content_snippet": "Sigma is...",
                        "relevance_score": 0.91,
                    }
                ],
                "model_used": "llama3",
                "search_results_count": 1,
                "cached": False,
                "duration_ms": 1200,
            },
        )

        result = client.ask("what is a sigma rule", top_k=3)

        assert result.answer == "A detection rule format."
        assert len(result.citations) == 1
        assert result.citations[0].relevance_score == 0.91
        assert result.model_used == "llama3"

    def test_sends_api_key_header_and_body(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/query/ask",
            json={"query": "q", "answer": "a", "citations": []},
        )

        client.ask("q", top_k=7, use_cache=False)

        request = httpx_mock.get_request()
        assert request.headers["X-API-Key"] == "grim_agt_test"
        import json as _json

        body = _json.loads(request.content)
        assert body == {"query": "q", "top_k": 7, "use_cache": False}


class TestSearch:
    def test_parses_results(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/query/search",
            json={
                "query": "sigma",
                "results": [
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "document_title": "Primer",
                        "content": "Sigma is...",
                        "score": 0.8,
                    }
                ],
                "total_results": 1,
                "duration_ms": 40,
            },
        )

        result = client.search("sigma", top_k=10)

        assert result.total_results == 1
        assert result.results[0].document_title == "Primer"


class TestRecentDocuments:
    def test_requests_limit_and_parses_rows(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/documents?offset=0&limit=10",
            json={
                "documents": [
                    {
                        "id": "d1",
                        "title": "Notes",
                        "source_path": "/app/uploads/abc_notes.md",
                        "file_type": "md",
                        "storage_backend": "local",
                        "processing_status": "completed",
                        "size_bytes": 2048,
                        "created_at": "2026-09-21T10:00:00Z",
                        "updated_at": "2026-09-21T10:00:05Z",
                        "tag_count": 3,
                        "chunk_count": 7,
                    }
                ],
                "total": 1,
                "offset": 0,
                "limit": 10,
            },
        )

        result = client.recent_documents(limit=10)

        assert result.total == 1
        assert result.documents[0].chunk_count == 7


class TestUpload:
    def test_posts_multipart_and_parses_result(
        self, client, httpx_mock, tmp_path
    ) -> None:
        sample = tmp_path / "notes.md"
        sample.write_text("# hello")
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/ingest/upload",
            json={
                "file_path": "/app/uploads/abc_notes.md",
                "document_id": "doc-9",
                "status": "completed",
                "chunks_created": 2,
                "vectors_stored": 2,
                "tags_applied": 1,
                "error_message": None,
                "duration_ms": 900,
            },
        )

        result = client.upload(sample, auto_tag=True)

        assert result.document_id == "doc-9"
        assert result.chunks_created == 2
        request = httpx_mock.get_request()
        assert b"notes.md" in request.content
        assert request.headers["content-type"].startswith("multipart/form-data")


class TestErrorMapping:
    def test_connection_refused(self, client, httpx_mock) -> None:
        httpx_mock.add_exception(httpx.ConnectError("refused"))
        with pytest.raises(ConnectionFailed) as exc:
            client.search("x")
        assert BASE in exc.value.message

    def test_timeout(self, client, httpx_mock) -> None:
        httpx_mock.add_exception(httpx.ReadTimeout("slow"))
        with pytest.raises(TimedOut):
            client.ask("x")

    def test_401(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=401, json={"detail": "nope"})
        with pytest.raises(AuthFailed) as exc:
            client.search("x")
        assert "key" in exc.value.message.lower()

    def test_403(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=403, json={"detail": "tier"})
        with pytest.raises(RequestRejected):
            client.search("x")

    def test_413_reports_the_limit(self, client, httpx_mock, tmp_path) -> None:
        sample = tmp_path / "big.pdf"
        sample.write_bytes(b"x" * 16)
        httpx_mock.add_response(
            status_code=413, json={"detail": "File exceeds the maximum upload size"}
        )
        with pytest.raises(RequestRejected) as exc:
            client.upload(sample)
        assert "exceeds" in exc.value.message.lower()

    def test_415(self, client, httpx_mock, tmp_path) -> None:
        sample = tmp_path / "thing.pdf"
        sample.write_bytes(b"x")
        httpx_mock.add_response(
            status_code=415, json={"detail": "Unsupported file type: .pdf"}
        )
        with pytest.raises(RequestRejected) as exc:
            client.upload(sample)
        assert "Unsupported" in exc.value.message

    def test_422_flattens_validation_detail(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            status_code=422,
            json={
                "detail": [
                    {"loc": ["body", "top_k"], "msg": "must be <= 100", "type": "x"}
                ]
            },
        )
        with pytest.raises(RequestRejected) as exc:
            client.search("x")
        assert "top_k" in exc.value.message
        assert "\n" not in exc.value.message

    def test_429_carries_retry_after(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            status_code=429, json={"detail": "slow down"}, headers={"Retry-After": "30"}
        )
        with pytest.raises(RateLimited) as exc:
            client.search("x")
        assert exc.value.retry_after == 30
        assert "30" in exc.value.message

    def test_500(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=500, text="boom")
        with pytest.raises(ServerError):
            client.search("x")

    def test_unparseable_body(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=200, text="not json at all")
        with pytest.raises(MalformedResponse):
            client.search("x")

    def test_schema_mismatch(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=200, json={"unexpected": True})
        with pytest.raises(MalformedResponse):
            client.recent_documents()


class TestUploadPreflight:
    def test_rejects_missing_file_without_a_request(self, client, tmp_path) -> None:
        with pytest.raises(RequestRejected):
            client.upload(tmp_path / "nope.pdf")

    def test_rejects_oversized_file_without_a_request(self, tmp_path) -> None:
        cfg = GuiConfig(base_url=BASE, api_key="k", max_upload_bytes=4)
        c = GrimoireClient(cfg)
        sample = tmp_path / "big.txt"
        sample.write_bytes(b"12345678")
        try:
            with pytest.raises(RequestRejected) as exc:
                c.upload(sample)
            assert "exceeds" in exc.value.message.lower()
        finally:
            c.close()

    def test_rejects_unsupported_extension_without_a_request(
        self, client, tmp_path
    ) -> None:
        sample = tmp_path / "thing.exe"
        sample.write_bytes(b"MZ")
        with pytest.raises(RequestRejected) as exc:
            client.upload(sample)
        assert ".exe" in exc.value.message
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_gui_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'grimoire.gui.client'`.

- [ ] **Step 3: Write the error module**

`grimoire/gui/errors.py`:

```python
"""Error types surfaced to the GUI.

Widgets only ever see these.  Every httpx exception, HTTP status, and
validation failure is translated here into text a person can act on; the
original exception goes to the log, never to the screen.
"""

from __future__ import annotations


class GuiError(Exception):
    """Base class for every failure the GUI is expected to display.

    Attributes:
        message: Text to show the user.  Complete sentence, no traceback.
        retry_after: Seconds to wait before retrying, when the server said.
    """

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class ConnectionFailed(GuiError):
    """The API could not be reached at all."""


class TimedOut(GuiError):
    """The API accepted the connection but did not answer in time."""


class AuthFailed(GuiError):
    """The API key is missing or rejected."""


class RequestRejected(GuiError):
    """The API refused this particular request (403, 404, 413, 415, 422)."""


class RateLimited(GuiError):
    """The API key's rate limit is exhausted."""


class ServerError(GuiError):
    """The API failed internally."""


class MalformedResponse(GuiError):
    """The API answered with something this client cannot parse."""
```

- [ ] **Step 4: Write the client**

`grimoire/gui/client.py`:

```python
"""Synchronous HTTP client for the Grimoire REST API.

Sync on purpose: Qt owns the event loop, and every call from the GUI runs on
a worker thread rather than in an asyncio task.  Responses are parsed with the
server's own Pydantic models, so the wire format has exactly one definition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

import httpx
from loguru import logger
from pydantic import BaseModel, ValidationError

from grimoire.api.schemas import (
    DocumentListResponse,
    IngestResultResponse,
    QueryResponse,
    SearchResponse,
)
from grimoire.gui.config import SUPPORTED_EXTENSIONS, GuiConfig
from grimoire.gui.errors import (
    AuthFailed,
    ConnectionFailed,
    GuiError,
    MalformedResponse,
    RateLimited,
    RequestRejected,
    ServerError,
    TimedOut,
)

ModelT = TypeVar("ModelT", bound=BaseModel)

_API_PREFIX = "/api/v1"


class GrimoireClient:
    """Typed access to the endpoints the desktop client needs.

    Every public method raises only GuiError subclasses.  That is the
    contract the widgets depend on: they never handle httpx or pydantic
    exceptions.
    """

    def __init__(self, config: GuiConfig) -> None:
        self.config = config
        self._http = httpx.Client(
            base_url=config.base_url,
            headers=self._headers(config),
            timeout=httpx.Timeout(
                connect=config.connect_timeout, read=config.read_timeout,
                write=config.read_timeout, pool=config.connect_timeout,
            ),
        )

    @staticmethod
    def _headers(config: GuiConfig) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if config.api_key:
            headers["X-API-Key"] = config.api_key
        return headers

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._http.close()

    # -- endpoints ---------------------------------------------------------

    def health(self) -> bool:
        """Whether /health answers 200.  Never raises."""
        try:
            response = self._http.get("/health", timeout=self.config.connect_timeout)
        except httpx.HTTPError as exc:
            logger.debug(f"Health check failed: {exc}")
            return False
        return response.status_code == 200

    def ask(
        self, query: str, *, top_k: int = 5, use_cache: bool = True
    ) -> QueryResponse:
        """Run the full RAG pipeline: retrieval plus a generated answer."""
        payload = {"query": query, "top_k": top_k, "use_cache": use_cache}
        response = self._request(
            "POST",
            f"{_API_PREFIX}/query/ask",
            json=payload,
            timeout=self.config.long_read_timeout,
        )
        return self._parse(response, QueryResponse)

    def search(self, query: str, *, top_k: int = 10) -> SearchResponse:
        """Retrieval only — no LLM in the path, so this is the fast check."""
        payload = {"query": query, "top_k": top_k}
        response = self._request("POST", f"{_API_PREFIX}/query/search", json=payload)
        return self._parse(response, SearchResponse)

    def recent_documents(self, *, limit: int = 10) -> DocumentListResponse:
        """Most recently created documents.

        The endpoint already orders by created_at descending, so no
        client-side sorting is needed.
        """
        response = self._request(
            "GET", f"{_API_PREFIX}/documents", params={"offset": 0, "limit": limit}
        )
        return self._parse(response, DocumentListResponse)

    def upload(self, path: Path, *, auto_tag: bool = True) -> IngestResultResponse:
        """Upload a local file and ingest it.

        Preflight checks run before any bytes leave the machine, so an
        obviously doomed upload costs nothing and reports immediately.
        """
        self._preflight_upload(path)
        with path.open("rb") as handle:
            response = self._request(
                "POST",
                f"{_API_PREFIX}/ingest/upload",
                files={"file": (path.name, handle, "application/octet-stream")},
                data={"auto_tag": "true" if auto_tag else "false"},
                timeout=self.config.long_read_timeout,
            )
        return self._parse(response, IngestResultResponse)

    # -- internals ---------------------------------------------------------

    def _preflight_upload(self, path: Path) -> None:
        """Reject a file the server is certain to refuse."""
        if not path.is_file():
            raise RequestRejected(f"Not a file: {path}")
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise RequestRejected(
                f"Unsupported file type: {suffix or '(no extension)'}"
            )
        size = path.stat().st_size
        if size > self.config.max_upload_bytes:
            limit_mb = self.config.max_upload_bytes / (1024 * 1024)
            raise RequestRejected(
                f"{path.name} exceeds the {limit_mb:.0f} MB upload limit"
            )

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform a request, translating transport failures and bad statuses."""
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            logger.warning(f"{method} {url} timed out: {exc}")
            raise TimedOut(
                "The request timed out. The API may be busy generating an answer."
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(f"{method} {url} failed to connect: {exc}")
            raise ConnectionFailed(
                f"Cannot reach the Grimoire API at {self.config.base_url} "
                "- is the stack running?"
            ) from exc

        if response.status_code >= 400:
            raise self._error_for(response)
        return response

    def _error_for(self, response: httpx.Response) -> GuiError:
        """Map an error response to the matching GuiError."""
        status = response.status_code
        detail = self._detail(response)

        if status == 401:
            return AuthFailed("API key rejected. Check GRIMOIRE_API_KEY.")
        if status == 403:
            return RequestRejected(
                f"This API key is not permitted to do that: {detail}"
            )
        if status == 404:
            return RequestRejected(f"Not found: {detail}")
        if status == 429:
            retry_after = self._retry_after(response)
            suffix = f" Retry in {retry_after}s." if retry_after else ""
            return RateLimited(f"Rate limited.{suffix}", retry_after=retry_after)
        if status >= 500:
            logger.error(f"API returned {status}: {response.text[:500]}")
            return ServerError("Grimoire API error - check the API logs.")
        return RequestRejected(detail)

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return int(float(raw))
        except ValueError:
            return None

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        """Flatten FastAPI's error body to one line.

        422 bodies are a list of per-field objects; anything multi-line would
        break the single-line error labels in the UI.
        """
        try:
            body = response.json()
        except ValueError:
            return response.text.strip()[:200] or f"HTTP {response.status_code}"

        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            parts = []
            for item in detail:
                if not isinstance(item, dict):
                    continue
                location = ".".join(str(part) for part in item.get("loc", [])[1:])
                parts.append(f"{location}: {item.get('msg', '')}".strip(": "))
            if parts:
                return "; ".join(parts)
        return f"HTTP {response.status_code}"

    @staticmethod
    def _parse(response: httpx.Response, model: type[ModelT]) -> ModelT:
        """Validate a successful response against the server's own schema."""
        try:
            return model.model_validate(response.json())
        except ValueError as exc:  # ValidationError and json decode both subclass it
            logger.error(
                f"Could not parse {model.__name__} from {response.url}: {exc}"
            )
            raise MalformedResponse(
                "Unexpected response from the API. It may be a different version."
            ) from exc
```

Note on the `except ValueError` in `_parse`: Pydantic's `ValidationError` subclasses `ValueError`, and `response.json()` raises `json.JSONDecodeError`, which also subclasses it. One clause covers both; the explicit `ValidationError` import stays for readers and for mypy's benefit — if ruff flags it as unused, use it in the type annotation of a local variable rather than deleting the behaviour.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_gui_client.py -v
```

Expected: PASS. If `httpx_mock` complains about unrequested responses, add `httpx_mock.reset()` semantics per `pytest-httpx`'s current version, or mark the fixture `assert_all_responses_were_requested=False` where a preflight test makes no request.

- [ ] **Step 6: Type-check and lint**

```bash
uv run mypy grimoire/gui/
uv run ruff check grimoire/gui/ tests/test_gui_client.py
uv run black --check grimoire/gui/ tests/test_gui_client.py
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add grimoire/gui/client.py grimoire/gui/errors.py tests/test_gui_client.py
git commit -m "$(cat <<'EOF'
Add GUI API client with human-readable error mapping

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Worker plumbing, main window, entry point

**Files:**
- Create: `grimoire/gui/workers.py`, `grimoire/gui/app.py`, `grimoire/gui/__main__.py`, `grimoire/gui/widgets/__init__.py`, `grimoire/gui/widgets/connection_bar.py`
- Create: `tests/test_gui_app.py`
- Modify: `tests/conftest.py` (Qt offscreen fixture)

**Interfaces:**
- Consumes: `GuiConfig`, `GrimoireClient`, `GuiError`.
- Produces:
  - `WorkerSignals(QObject)` with `finished = Signal(object)` and `failed = Signal(object)`
  - `ApiWorker(QRunnable)` — `__init__(self, fn: Callable[[], object])`, exposes `.signals`
  - `run_api_call(pool: QThreadPool, fn: Callable[[], object], on_ok: Callable[[Any], None], on_err: Callable[[GuiError], None]) -> None`
  - `ConnectionBar(QWidget)` with `api_key_entered = Signal(str)` and `set_state(configured: bool, base_url: str) -> None`
  - `MainWindow(QMainWindow)` — `__init__(self, client: GrimoireClient, config: GuiConfig)`, attributes `.tabs: QTabWidget`, `.pool: QThreadPool`, method `.show_error(message: str) -> None`
  - `main() -> int` in `__main__.py`

**Context:** This task produces a window that runs and shows an (empty) tab area. Tasks 7–10 each add one tab, so the application is launchable and testable from here on.

`ApiWorker` catches `GuiError` and emits it; it also catches `Exception` as a backstop, because an uncaught exception inside a `QRunnable` tears down the pool thread silently and the UI hangs with a spinner forever.

- [ ] **Step 1: Add the Qt test fixture**

Append to `tests/conftest.py`:

```python
# =============================================================================
# Qt Fixtures
# =============================================================================


@pytest.fixture(scope="session", autouse=True)
def _qt_offscreen() -> None:
    """Force Qt's offscreen platform so GUI tests run headless in CI.

    Set before any QApplication is constructed; pytest-qt reads it when it
    creates the app for the first qtbot fixture.
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_gui_app.py`:

```python
"""Tests for GUI worker plumbing and the main window shell."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QThreadPool  # noqa: E402

from grimoire.gui.app import MainWindow  # noqa: E402
from grimoire.gui.config import GuiConfig  # noqa: E402
from grimoire.gui.errors import AuthFailed  # noqa: E402
from grimoire.gui.workers import ApiWorker  # noqa: E402


class _StubClient:
    """Stands in for GrimoireClient in window tests."""

    def __init__(self, config: GuiConfig) -> None:
        self.config = config

    def close(self) -> None:
        pass


class TestApiWorker:
    def test_emits_result_on_success(self, qtbot) -> None:
        worker = ApiWorker(lambda: "done")
        with qtbot.waitSignal(worker.signals.finished, timeout=2000) as blocker:
            QThreadPool.globalInstance().start(worker)
        assert blocker.args == ["done"]

    def test_emits_gui_error_on_failure(self, qtbot) -> None:
        def boom() -> str:
            raise AuthFailed("API key rejected.")

        worker = ApiWorker(boom)
        with qtbot.waitSignal(worker.signals.failed, timeout=2000) as blocker:
            QThreadPool.globalInstance().start(worker)
        assert isinstance(blocker.args[0], AuthFailed)

    def test_wraps_unexpected_exception_as_gui_error(self, qtbot) -> None:
        from grimoire.gui.errors import GuiError

        def boom() -> str:
            raise RuntimeError("kaboom")

        worker = ApiWorker(boom)
        with qtbot.waitSignal(worker.signals.failed, timeout=2000) as blocker:
            QThreadPool.globalInstance().start(worker)
        error = blocker.args[0]
        assert isinstance(error, GuiError)
        assert "kaboom" not in error.message, "raw exception text must not leak"


class TestMainWindow:
    def test_opens_with_a_tab_area(self, qtbot) -> None:
        config = GuiConfig(base_url="http://testapi:8001", api_key="k")
        window = MainWindow(_StubClient(config), config)
        qtbot.addWidget(window)
        assert window.tabs is not None
        assert "Grimoire" in window.windowTitle()

    def test_shows_unconfigured_state_without_a_key(self, qtbot) -> None:
        config = GuiConfig(base_url="http://testapi:8001", api_key=None)
        window = MainWindow(_StubClient(config), config)
        qtbot.addWidget(window)
        assert "GRIMOIRE_API_KEY" in window.connection_bar.status_text()

    def test_status_bar_shows_errors(self, qtbot) -> None:
        config = GuiConfig(base_url="http://testapi:8001", api_key="k")
        window = MainWindow(_StubClient(config), config)
        qtbot.addWidget(window)
        window.show_error("Cannot reach the Grimoire API")
        assert "Cannot reach" in window.statusBar().currentMessage()
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/test_gui_app.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'grimoire.gui.workers'`.

- [ ] **Step 4: Write the worker module**

`grimoire/gui/workers.py`:

```python
"""Run blocking client calls off the GUI thread.

Qt's event loop is single-threaded: any call that waits on the network from
the GUI thread freezes the window.  Everything the client does goes through
an ApiWorker on a QThreadPool instead, and the result comes back as a signal
delivered on the GUI thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from grimoire.gui.errors import GuiError


class WorkerSignals(QObject):
    """Signals emitted by an ApiWorker.

    QRunnable is not a QObject, so the signals live on this companion.
    """

    finished = Signal(object)
    failed = Signal(object)


class ApiWorker(QRunnable):
    """Call one function on a pool thread and report the outcome.

    Args:
        fn: Zero-argument callable, typically a lambda closing over a
            GrimoireClient method and its arguments.
    """

    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self._fn = fn
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        """Execute the call, emitting finished or failed exactly once."""
        try:
            result = self._fn()
        except GuiError as exc:
            self.signals.failed.emit(exc)
        except Exception as exc:  # noqa: BLE001 - backstop, see below
            # An exception escaping a QRunnable kills the pool thread without
            # a word and leaves the UI spinning forever.  Log the real cause,
            # show the user something generic.
            logger.exception(f"Unexpected error in GUI worker: {exc}")
            self.signals.failed.emit(
                GuiError("Something went wrong. See the log for details.")
            )
        else:
            self.signals.finished.emit(result)


def run_api_call(
    pool: QThreadPool,
    fn: Callable[[], object],
    on_ok: Callable[[Any], None],
    on_err: Callable[[GuiError], None],
) -> None:
    """Submit a client call and route its outcome to two slots.

    Args:
        pool: The window's thread pool.
        fn: The call to make.
        on_ok: Receives the return value, on the GUI thread.
        on_err: Receives a GuiError, on the GUI thread.
    """
    worker = ApiWorker(fn)
    worker.signals.finished.connect(on_ok)
    worker.signals.failed.connect(on_err)
    pool.start(worker)
```

If ruff's `BLE001` is not in the selected rule set, drop the `# noqa` comment but keep the explanatory comment.

- [ ] **Step 5: Write the connection bar**

`grimoire/gui/widgets/__init__.py`:

```python
"""Widgets for the Grimoire desktop client."""
```

`grimoire/gui/widgets/connection_bar.py`:

```python
"""Connection state strip shown above the tabs."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)


class ConnectionBar(QWidget):
    """Shows which API the client talks to and whether it has a key.

    A key pasted here is kept in memory for the session only.  Nothing in
    this widget writes it to QSettings or to a file: a plaintext credential
    in ~/.config is a worse default than retyping one.
    """

    # Declared before __init__: Qt resolves signals as class attributes at
    # metaclass time, so one assigned later in the body never connects.
    api_key_entered = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._status = QLabel()
        self._key_field = QLineEdit()
        self._key_field.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_field.setPlaceholderText("Paste an API key for this session")
        self._apply = QPushButton("Use key")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(self._status, stretch=1)
        layout.addWidget(self._key_field)
        layout.addWidget(self._apply)

        self._apply.clicked.connect(self._emit_key)
        self._key_field.returnPressed.connect(self._emit_key)

    def _emit_key(self) -> None:
        key = self._key_field.text().strip()
        if key:
            self.api_key_entered.emit(key)
            self._key_field.clear()

    def set_state(self, configured: bool, base_url: str) -> None:
        """Update the displayed connection state.

        Args:
            configured: Whether an API key is present.
            base_url: The API root currently in use.
        """
        if configured:
            self._status.setText(f"Connected to {base_url}")
            self._key_field.setVisible(False)
            self._apply.setVisible(False)
        else:
            self._status.setText(
                f"No API key. Set GRIMOIRE_API_KEY (API at {base_url}) "
                "or paste one here."
            )
            self._key_field.setVisible(True)
            self._apply.setVisible(True)

    def status_text(self) -> str:
        """Current status line. Used by tests and by the status bar."""
        return self._status.text()
```

Move the `api_key_entered = Signal(str)` declaration to the top of the class body, directly under the docstring — a Qt signal must be a class attribute declared before `__init__` runs, and add `Signal` to the `PySide6.QtCore` import:

```python
from PySide6.QtCore import Signal
```

- [ ] **Step 6: Write the main window**

`grimoire/gui/app.py`:

```python
"""Main window for the Grimoire desktop client."""

from __future__ import annotations

from typing import Any

from loguru import logger
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from grimoire.gui.config import GuiConfig
from grimoire.gui.widgets.connection_bar import ConnectionBar

_ERROR_TIMEOUT_MS = 15_000
_SHUTDOWN_GRACE_MS = 3_000


class MainWindow(QMainWindow):
    """Hosts the connection bar, the tab area, and the shared thread pool.

    Args:
        client: The API client every tab shares.
        config: Configuration the client was built from.
    """

    def __init__(self, client: Any, config: GuiConfig) -> None:
        super().__init__()
        self.client = client
        self.config = config
        self.pool = QThreadPool()
        # The pipeline is CPU- and GPU-bound server side; more client
        # threads would only queue deeper on the API.
        self.pool.setMaxThreadCount(4)

        self.setWindowTitle("Grimoire")
        self.resize(1100, 760)

        self.connection_bar = ConnectionBar()
        self.connection_bar.set_state(config.is_configured, config.base_url)
        self.connection_bar.api_key_entered.connect(self._on_api_key_entered)

        self.tabs = QTabWidget()

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.connection_bar)
        layout.addWidget(self.tabs, stretch=1)
        self.setCentralWidget(container)

        self.statusBar().showMessage("Ready")

    def show_error(self, message: str) -> None:
        """Surface an error in the status bar.

        Tabs also show their own inline error; this is the cross-tab trail.
        """
        self.statusBar().showMessage(message, _ERROR_TIMEOUT_MS)

    def _on_api_key_entered(self, key: str) -> None:
        """Rebuild the client around a session key pasted by the user."""
        from grimoire.gui.client import GrimoireClient

        self.config = self.config.with_api_key(key)
        old_client = self.client
        self.client = GrimoireClient(self.config)
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            setter = getattr(tab, "set_client", None)
            if callable(setter):
                setter(self.client)
        old_client.close()
        self.connection_bar.set_state(self.config.is_configured, self.config.base_url)
        self.statusBar().showMessage("API key updated for this session", 5000)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        """Drain workers and stop child processes before the window closes."""
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            shutdown = getattr(tab, "shutdown", None)
            if callable(shutdown):
                shutdown()
        if not self.pool.waitForDone(_SHUTDOWN_GRACE_MS):
            logger.warning("GUI worker pool did not drain before shutdown")
        self.client.close()
        super().closeEvent(event)
```

`closeEvent` is Qt's camelCase override, which ruff's `N802` flags; the `noqa` is the correct response since renaming it would break the override.

- [ ] **Step 7: Write the entry point**

`grimoire/gui/__main__.py`:

```python
"""Entry point for the Grimoire desktop client.

Run with ``grimoire-gui`` after ``uv sync --extra gui``.
"""

from __future__ import annotations

import sys
from uuid import uuid4

from loguru import logger


def main() -> int:
    """Start the GUI.

    Returns:
        The Qt application's exit code, or 1 when PySide6 is not installed.
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "PySide6 is not installed. Run: uv sync --extra gui",
            file=sys.stderr,
        )
        return 1

    from grimoire.gui.app import MainWindow
    from grimoire.gui.client import GrimoireClient
    from grimoire.gui.config import GuiConfig

    session_id = uuid4().hex[:12]
    # Bound once so every record from this launch can be isolated in the log,
    # matching the project's context-in-log-records convention.
    logger.configure(extra={"session_id": session_id})
    logger.info(f"Starting Grimoire GUI (session {session_id})")

    config = GuiConfig.from_env()
    client = GrimoireClient(config)

    app = QApplication(sys.argv)
    app.setApplicationName("Grimoire")
    window = MainWindow(client, config)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 8: Run the tests to verify they pass**

```bash
uv run pytest tests/test_gui_app.py -v
```

Expected: PASS, all six tests.

- [ ] **Step 9: Launch it once by hand**

```bash
uv run grimoire-gui
```

Expected: a window titled "Grimoire" opens with the connection bar and an empty tab area. If it does not appear, check `echo "$DISPLAY $WAYLAND_DISPLAY"` — WSLg is the prerequisite from the spec. Close the window; the process should exit cleanly with no traceback.

- [ ] **Step 10: Type-check and lint**

```bash
uv run mypy grimoire/gui/
uv run ruff check grimoire/gui/ tests/test_gui_app.py tests/conftest.py
uv run black --check grimoire/gui/ tests/test_gui_app.py tests/conftest.py
```

Expected: clean.

- [ ] **Step 11: Commit**

```bash
git add grimoire/gui/ tests/test_gui_app.py tests/conftest.py
git commit -m "$(cat <<'EOF'
Add GUI main window, worker pool, and entry point

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Search / Ask tab

**Files:**
- Create: `grimoire/gui/widgets/citation_card.py`, `grimoire/gui/widgets/search_tab.py`
- Modify: `grimoire/gui/app.py` (register the tab)
- Test: `tests/test_gui_search_tab.py`

**Interfaces:**
- Consumes: `GrimoireClient.ask`, `GrimoireClient.search`, `run_api_call`, `MainWindow.show_error`.
- Produces:
  - `CitationCard(QWidget)` — `__init__(self, title: str, score: float, snippet: str, document_id: str)`
  - `SearchTab(QWidget)` — `__init__(self, client: Any, pool: QThreadPool, on_error: Callable[[str], None])`, methods `set_client(client) -> None`, `submit() -> None`, attributes `.query_field`, `.ask_radio`, `.search_radio`, `.answer_view`, `.results_layout`, `.error_label`, `.footer_label`

**Context:** The Search mode exists so retrieval can be checked without paying for generation — it is the tab's latency control, not a nicety. Both modes render the same `CitationCard`, which is why the card is its own file.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gui_search_tab.py`:

```python
"""Tests for the Search/Ask tab."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QThreadPool  # noqa: E402

from grimoire.api.schemas import (  # noqa: E402
    CitationResponse,
    QueryResponse,
    SearchResponse,
    SearchResultItem,
)
from grimoire.gui.errors import ConnectionFailed  # noqa: E402
from grimoire.gui.widgets.search_tab import SearchTab  # noqa: E402


class _StubClient:
    def __init__(self) -> None:
        self.ask_calls: list[tuple[str, int, bool]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.ask_error: Exception | None = None

    def ask(self, query, *, top_k=5, use_cache=True):
        self.ask_calls.append((query, top_k, use_cache))
        if self.ask_error:
            raise self.ask_error
        return QueryResponse(
            query=query,
            answer="Because of X.",
            citations=[
                CitationResponse(
                    document_id="doc-1",
                    document_title="Primer",
                    chunk_id="c1",
                    chunk_index=0,
                    content_snippet="X is...",
                    relevance_score=0.77,
                )
            ],
            model_used="llama3",
            search_results_count=1,
            cached=True,
            duration_ms=1500,
        )

    def search(self, query, *, top_k=10):
        self.search_calls.append((query, top_k))
        return SearchResponse(
            query=query,
            results=[
                SearchResultItem(
                    chunk_id="c1",
                    document_id="doc-1",
                    document_title="Primer",
                    content="X is...",
                    score=0.66,
                )
            ],
            total_results=1,
            duration_ms=40,
        )


def _make_tab(qtbot, client=None, errors=None):
    tab = SearchTab(client or _StubClient(), QThreadPool(), errors.append if errors is not None else (lambda _m: None))
    qtbot.addWidget(tab)
    return tab


def _card_count(tab) -> int:
    return tab.results_layout.count()


class TestAskMode:
    def test_renders_answer_and_cards(self, qtbot) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.query_field.setText("why x")
        tab.ask_radio.setChecked(True)

        tab.submit()
        qtbot.waitUntil(lambda: _card_count(tab) > 0, timeout=3000)

        assert "Because of X." in tab.answer_view.toPlainText()
        assert client.ask_calls == [("why x", 5, True)]
        assert "llama3" in tab.footer_label.text()
        assert "cached" in tab.footer_label.text().lower()

    def test_passes_top_k_and_cache_flag(self, qtbot) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.query_field.setText("why x")
        tab.top_k_spin.setValue(12)
        tab.cache_checkbox.setChecked(False)

        tab.submit()
        qtbot.waitUntil(lambda: bool(client.ask_calls), timeout=3000)

        assert client.ask_calls == [("why x", 12, False)]


class TestSearchMode:
    def test_renders_cards_without_answer_panel(self, qtbot) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.query_field.setText("x")
        tab.search_radio.setChecked(True)

        tab.submit()
        qtbot.waitUntil(lambda: _card_count(tab) > 0, timeout=3000)

        assert client.search_calls == [("x", 10)]
        assert not tab.answer_view.isVisible()


class TestEmptyAndErrorStates:
    def test_blank_query_makes_no_request(self, qtbot) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.query_field.setText("   ")

        tab.submit()

        assert client.ask_calls == []
        assert client.search_calls == []

    def test_error_shows_inline_and_reenables_button(self, qtbot) -> None:
        client = _StubClient()
        client.ask_error = ConnectionFailed("Cannot reach the Grimoire API")
        errors: list[str] = []
        tab = _make_tab(qtbot, client, errors)
        tab.query_field.setText("why x")

        tab.submit()
        qtbot.waitUntil(lambda: bool(tab.error_label.text()), timeout=3000)

        assert "Cannot reach" in tab.error_label.text()
        assert tab.submit_button.isEnabled()
        assert errors and "Cannot reach" in errors[0]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_gui_search_tab.py -v
```

Expected: FAIL — no module `grimoire.gui.widgets.search_tab`.

- [ ] **Step 3: Write the citation card**

`grimoire/gui/widgets/citation_card.py`:

```python
"""One retrieved chunk, rendered as a card.

Shared by both query modes: an Ask shows the chunks its answer rests on, a
Search shows the same chunks with no answer above them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

_SNIPPET_LIMIT = 600


class CitationCard(QFrame):
    """A source chunk with its score and a copyable document id.

    Args:
        title: Document title, or a placeholder when the document has none.
        score: Relevance score, shown to two decimals.
        snippet: Chunk text; truncated for display.
        document_id: Copied to the clipboard by the card's button, so a
            result can be followed up with the CLI or the API.
    """

    def __init__(
        self, title: str, score: float, snippet: str, document_id: str
    ) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.document_id = document_id

        header = QHBoxLayout()
        title_label = QLabel(f"<b>{title or '(untitled)'}</b>")
        title_label.setWordWrap(True)
        score_label = QLabel(f"{score:.2f}")
        copy_button = QPushButton("Copy ID")
        copy_button.setToolTip(document_id)
        copy_button.clicked.connect(self._copy_id)
        header.addWidget(title_label, stretch=1)
        header.addWidget(score_label)
        header.addWidget(copy_button)

        body = QLabel(self._truncate(snippet))
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            body.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
        )

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(body)

    @staticmethod
    def _truncate(snippet: str) -> str:
        text = snippet.strip()
        if len(text) <= _SNIPPET_LIMIT:
            return text
        return f"{text[:_SNIPPET_LIMIT]}…"

    def _copy_id(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.document_id)
```

- [ ] **Step 4: Write the search tab**

`grimoire/gui/widgets/search_tab.py`:

```python
"""Search / Ask tab.

Two modes over one query box.  Ask runs the full RAG pipeline; Search runs
retrieval only, which is the fast way to check whether the right chunks come
back before spending an LLM generation on them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from grimoire.api.schemas import QueryResponse, SearchResponse
from grimoire.gui.errors import GuiError
from grimoire.gui.widgets.citation_card import CitationCard
from grimoire.gui.workers import run_api_call


class SearchTab(QWidget):
    """Query the corpus and inspect what retrieval returned.

    Args:
        client: GrimoireClient (or a stub in tests).
        pool: Shared thread pool; no request runs on the GUI thread.
        on_error: Callback that puts a message in the window's status bar.
    """

    def __init__(
        self, client: Any, pool: QThreadPool, on_error: Callable[[str], None]
    ) -> None:
        super().__init__()
        self._client = client
        self._pool = pool
        self._on_error = on_error

        self.query_field = QLineEdit()
        self.query_field.setPlaceholderText("Ask a question, or search for a phrase")
        self.submit_button = QPushButton("Run")
        self.ask_radio = QRadioButton("Ask")
        self.search_radio = QRadioButton("Search")
        self.ask_radio.setChecked(True)
        self.ask_radio.setToolTip("Retrieval plus a generated answer")
        self.search_radio.setToolTip("Retrieval only - no LLM, much faster")
        self.top_k_spin = QSpinBox()
        self.top_k_spin.setRange(1, 100)
        self.top_k_spin.setValue(5)
        self.top_k_spin.setPrefix("top_k ")
        self.cache_checkbox = QCheckBox("Use cache")
        self.cache_checkbox.setChecked(True)

        controls = QHBoxLayout()
        controls.addWidget(self.ask_radio)
        controls.addWidget(self.search_radio)
        controls.addWidget(self.top_k_spin)
        controls.addWidget(self.cache_checkbox)
        controls.addStretch(1)

        query_row = QHBoxLayout()
        query_row.addWidget(self.query_field, stretch=1)
        query_row.addWidget(self.submit_button)

        self.answer_view = QTextBrowser()
        self.answer_view.setOpenExternalLinks(False)
        self.answer_view.setMinimumHeight(180)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.footer_label = QLabel()

        results_container = QWidget()
        self.results_layout = QVBoxLayout(results_container)
        self.results_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(results_container)

        layout = QVBoxLayout(self)
        layout.addLayout(query_row)
        layout.addLayout(controls)
        layout.addWidget(self.error_label)
        layout.addWidget(self.answer_view)
        layout.addWidget(self.footer_label)
        layout.addWidget(scroll, stretch=1)

        self.submit_button.clicked.connect(self.submit)
        self.query_field.returnPressed.connect(self.submit)
        self.ask_radio.toggled.connect(self._sync_mode)
        self._sync_mode()

    def set_client(self, client: Any) -> None:
        """Swap in a client rebuilt around a new session key."""
        self._client = client

    def _sync_mode(self) -> None:
        """Hide the answer panel in Search mode: there is no answer."""
        self.answer_view.setVisible(self.ask_radio.isChecked())
        self.cache_checkbox.setEnabled(self.ask_radio.isChecked())

    def submit(self) -> None:
        """Run the current mode's query, unless the box is empty."""
        query = self.query_field.text().strip()
        if not query:
            return

        self._clear_results()
        self.error_label.clear()
        self.footer_label.setText("Working…")
        self.submit_button.setEnabled(False)

        if self.ask_radio.isChecked():
            top_k = self.top_k_spin.value()
            use_cache = self.cache_checkbox.isChecked()
            run_api_call(
                self._pool,
                lambda: self._client.ask(query, top_k=top_k, use_cache=use_cache),
                self._on_ask_result,
                self._on_failure,
            )
        else:
            top_k = self.top_k_spin.value()
            run_api_call(
                self._pool,
                lambda: self._client.search(query, top_k=top_k),
                self._on_search_result,
                self._on_failure,
            )

    def _on_ask_result(self, result: QueryResponse) -> None:
        self.submit_button.setEnabled(True)
        self.answer_view.setMarkdown(result.answer or "_(no answer returned)_")
        for citation in result.citations:
            self.results_layout.addWidget(
                CitationCard(
                    citation.document_title or "(untitled)",
                    citation.relevance_score,
                    citation.content_snippet,
                    citation.document_id,
                )
            )
        cached = "cached" if result.cached else "fresh"
        self.footer_label.setText(
            f"{len(result.citations)} sources · {result.model_used} · "
            f"{cached} · {result.duration_ms} ms"
        )
        if not result.citations:
            self._show_empty("No sources matched this question.")

    def _on_search_result(self, result: SearchResponse) -> None:
        self.submit_button.setEnabled(True)
        for item in result.results:
            self.results_layout.addWidget(
                CitationCard(
                    item.document_title or "(untitled)",
                    item.score,
                    item.content,
                    item.document_id,
                )
            )
        self.footer_label.setText(
            f"{result.total_results} results · {result.duration_ms} ms"
        )
        if not result.results:
            self._show_empty("No chunks matched that search.")

    def _on_failure(self, error: GuiError) -> None:
        self.submit_button.setEnabled(True)
        self.footer_label.clear()
        self.error_label.setText(error.message)
        self._on_error(error.message)

    def _show_empty(self, message: str) -> None:
        self.results_layout.addWidget(QLabel(message))

    def _clear_results(self) -> None:
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
```

- [ ] **Step 5: Register the tab**

In `grimoire/gui/app.py`, after `self.tabs = QTabWidget()`:

```python
        from grimoire.gui.widgets.search_tab import SearchTab

        self.search_tab = SearchTab(self.client, self.pool, self.show_error)
        self.tabs.addTab(self.search_tab, "Search / Ask")
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest tests/test_gui_search_tab.py tests/test_gui_app.py -v
```

Expected: PASS.

- [ ] **Step 7: Check it by hand against a running API**

```bash
docker compose up -d api
GRIMOIRE_API_KEY=<a dev-tier key> uv run grimoire-gui
```

Ask a question you know the corpus can answer; confirm the answer renders and cards appear below it. Switch to Search and confirm it returns noticeably faster with no answer panel.

- [ ] **Step 8: Type-check and lint**

```bash
uv run mypy grimoire/gui/
uv run ruff check grimoire/gui/ tests/test_gui_search_tab.py
uv run black --check grimoire/gui/ tests/test_gui_search_tab.py
```

- [ ] **Step 9: Commit**

```bash
git add grimoire/gui/ tests/test_gui_search_tab.py
git commit -m "$(cat <<'EOF'
Add Search and Ask tab with source chunk cards

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Recent ingests tab

**Files:**
- Create: `grimoire/gui/widgets/recent_tab.py`
- Modify: `grimoire/gui/app.py` (register the tab)
- Test: `tests/test_gui_recent_tab.py`

**Interfaces:**
- Consumes: `GrimoireClient.recent_documents`, `run_api_call`.
- Produces: `RecentTab(QWidget)` — `__init__(self, client: Any, pool: QThreadPool, on_error: Callable[[str], None])`, methods `refresh() -> None`, `set_client(client) -> None`, `showEvent(event)`; attributes `.table: QTableWidget`, `.error_label`, `.refresh_button`

**Context:** No `QTimer` polling. Every API key carries a per-minute rate limit (`grimoire/api/auth.py:37`); a background poll spends that budget on a window nobody is looking at. Refresh happens on tab activation, on the button, and once when an ingest completes (wired in Task 9).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gui_recent_tab.py`:

```python
"""Tests for the Recent ingests tab."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QThreadPool  # noqa: E402

from grimoire.api.schemas import DocumentListResponse, DocumentResponse  # noqa: E402
from grimoire.gui.errors import AuthFailed  # noqa: E402
from grimoire.gui.widgets.recent_tab import RecentTab  # noqa: E402


def _doc(doc_id: str, status: str = "completed") -> DocumentResponse:
    return DocumentResponse(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_path=f"/app/uploads/{doc_id}.md",
        file_type="md",
        storage_backend="local",
        processing_status=status,
        size_bytes=4096,
        created_at="2026-09-21T10:00:00Z",
        updated_at="2026-09-21T10:00:01Z",
        tag_count=2,
        chunk_count=6,
    )


class _StubClient:
    def __init__(self, docs=None, error=None) -> None:
        self.calls = 0
        self.docs = docs if docs is not None else [_doc("a"), _doc("b")]
        self.error = error

    def recent_documents(self, *, limit=10):
        self.calls += 1
        if self.error:
            raise self.error
        return DocumentListResponse(
            documents=self.docs, total=len(self.docs), offset=0, limit=limit
        )


class TestRecentTab:
    def test_refresh_populates_rows(self, qtbot) -> None:
        client = _StubClient()
        tab = RecentTab(client, QThreadPool(), lambda _m: None)
        qtbot.addWidget(tab)

        tab.refresh()
        qtbot.waitUntil(lambda: tab.table.rowCount() == 2, timeout=3000)

        assert tab.table.item(0, 0).text() == "Doc a"
        assert tab.table.item(0, 3).text() == "6"  # chunk count column

    def test_failed_document_is_marked(self, qtbot) -> None:
        client = _StubClient(docs=[_doc("bad", status="failed")])
        tab = RecentTab(client, QThreadPool(), lambda _m: None)
        qtbot.addWidget(tab)

        tab.refresh()
        qtbot.waitUntil(lambda: tab.table.rowCount() == 1, timeout=3000)

        assert tab.table.item(0, 2).text() == "failed"

    def test_empty_corpus_shows_no_rows(self, qtbot) -> None:
        client = _StubClient(docs=[])
        tab = RecentTab(client, QThreadPool(), lambda _m: None)
        qtbot.addWidget(tab)

        tab.refresh()
        qtbot.waitUntil(lambda: bool(tab.error_label.text()), timeout=3000)

        assert tab.table.rowCount() == 0
        assert "No documents" in tab.error_label.text()

    def test_error_is_reported_inline_and_to_the_window(self, qtbot) -> None:
        errors: list[str] = []
        client = _StubClient(error=AuthFailed("API key rejected."))
        tab = RecentTab(client, QThreadPool(), errors.append)
        qtbot.addWidget(tab)

        tab.refresh()
        qtbot.waitUntil(lambda: bool(tab.error_label.text()), timeout=3000)

        assert "rejected" in tab.error_label.text()
        assert errors == ["API key rejected."]

    def test_showing_the_tab_refreshes_once(self, qtbot) -> None:
        client = _StubClient()
        tab = RecentTab(client, QThreadPool(), lambda _m: None)
        qtbot.addWidget(tab)

        tab.show()
        qtbot.waitUntil(lambda: client.calls >= 1, timeout=3000)

        assert client.calls == 1, "tab activation must not stack requests"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_gui_recent_tab.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Write the tab**

`grimoire/gui/widgets/recent_tab.py`:

```python
"""Recent ingests tab.

Shows the ten most recently created documents.  The API already orders by
created_at descending, so this widget never sorts.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from grimoire.api.schemas import DocumentListResponse, DocumentResponse
from grimoire.gui.errors import GuiError
from grimoire.gui.workers import run_api_call

_COLUMNS = ("Title", "Type", "Status", "Chunks", "Tags", "Size", "Created")
_ROW_LIMIT = 10
_FAILED_ROW_COLOR = QColor(120, 30, 30)


class RecentTab(QWidget):
    """The last ten documents the corpus absorbed.

    Args:
        client: GrimoireClient (or a stub in tests).
        pool: Shared thread pool.
        on_error: Callback that puts a message in the window's status bar.
    """

    def __init__(
        self, client: Any, pool: QThreadPool, on_error: Callable[[str], None]
    ) -> None:
        super().__init__()
        self._client = client
        self._pool = pool
        self._on_error = on_error
        self._in_flight = False

        self.refresh_button = QPushButton("Refresh")
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        top_row = QHBoxLayout()
        top_row.addWidget(self.error_label, stretch=1)
        top_row.addWidget(self.refresh_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self.table, stretch=1)

        self.refresh_button.clicked.connect(self.refresh)

    def set_client(self, client: Any) -> None:
        """Swap in a client rebuilt around a new session key."""
        self._client = client

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        """Refresh when the tab becomes visible.

        No QTimer poll: every key has a per-minute rate limit, and polling a
        window nobody is looking at spends it for nothing.
        """
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        """Re-request the ten most recent documents."""
        if self._in_flight:
            return
        self._in_flight = True
        self.refresh_button.setEnabled(False)
        self.error_label.setText("Loading…")
        run_api_call(
            self._pool,
            lambda: self._client.recent_documents(limit=_ROW_LIMIT),
            self._on_result,
            self._on_failure,
        )

    def _on_result(self, result: DocumentListResponse) -> None:
        self._in_flight = False
        self.refresh_button.setEnabled(True)
        self.table.setRowCount(len(result.documents))
        for row, document in enumerate(result.documents):
            self._fill_row(row, document)
        self.error_label.setText(
            "" if result.documents else "No documents ingested yet."
        )

    def _fill_row(self, row: int, document: DocumentResponse) -> None:
        values = (
            document.title or "(untitled)",
            document.file_type,
            document.processing_status,
            str(document.chunk_count),
            str(document.tag_count),
            _humanize_bytes(document.size_bytes),
            _format_timestamp(document.created_at),
        )
        failed = document.processing_status == "failed"
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(document.source_path)
            if failed:
                item.setForeground(QBrush(_FAILED_ROW_COLOR))
            if column in (3, 4, 5):
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            self.table.setItem(row, column, item)

    def _on_failure(self, error: GuiError) -> None:
        self._in_flight = False
        self.refresh_button.setEnabled(True)
        self.error_label.setText(error.message)
        self._on_error(error.message)


def _humanize_bytes(size: int) -> str:
    """Render a byte count in the largest unit that keeps it readable."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _format_timestamp(raw: str | None) -> str:
    """Render an API timestamp in local time, falling back to the raw string."""
    if not raw:
        return "-"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
```

- [ ] **Step 4: Register the tab**

In `grimoire/gui/app.py`, after the Search tab registration:

```python
        from grimoire.gui.widgets.recent_tab import RecentTab

        self.recent_tab = RecentTab(self.client, self.pool, self.show_error)
        self.tabs.addTab(self.recent_tab, "Recent ingests")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_gui_recent_tab.py -v
```

Expected: PASS, all five tests.

- [ ] **Step 6: Type-check and lint**

```bash
uv run mypy grimoire/gui/
uv run ruff check grimoire/gui/ tests/test_gui_recent_tab.py
uv run black --check grimoire/gui/ tests/test_gui_recent_tab.py
```

- [ ] **Step 7: Commit**

```bash
git add grimoire/gui/ tests/test_gui_recent_tab.py
git commit -m "$(cat <<'EOF'
Add recent ingests tab backed by the documents endpoint

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Drag-and-drop ingest tab

**Files:**
- Create: `grimoire/gui/widgets/ingest_tab.py`
- Modify: `grimoire/gui/app.py` (register the tab, wire the refresh signal)
- Test: `tests/test_gui_ingest_tab.py`

**Interfaces:**
- Consumes: `GrimoireClient.upload`, `SUPPORTED_EXTENSIONS`, `GuiConfig.max_upload_bytes`, `run_api_call`.
- Produces: `IngestTab(QWidget)` — `__init__(self, client: Any, config: GuiConfig, pool: QThreadPool, on_error: Callable[[str], None])`, signal `ingest_completed = Signal()`, methods `enqueue(paths: list[Path]) -> None`, `set_client(client) -> None`; attributes `.queue_table: QTableWidget`, `.progress: QProgressBar`, `.auto_tag_checkbox`, `.error_label`

**Context:** Files upload one at a time. The pipeline is CPU- and GPU-bound server side, so parallel uploads would only queue deeper on the API while making progress reporting meaningless. Progress is measured in **files**, not bytes: byte-level progress needs a streaming read callback, and the slow part is parsing and embedding, not the local-network transfer.

Rejected files stay visible in the queue with the reason. A file that silently disappears from a drop is worse than one that explains itself.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gui_ingest_tab.py`:

```python
"""Tests for the drag-and-drop ingest tab."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QThreadPool  # noqa: E402

from grimoire.api.schemas import IngestResultResponse  # noqa: E402
from grimoire.gui.config import GuiConfig  # noqa: E402
from grimoire.gui.errors import RequestRejected  # noqa: E402
from grimoire.gui.widgets.ingest_tab import IngestTab  # noqa: E402


class _StubClient:
    def __init__(self, error_for: str | None = None) -> None:
        self.uploaded: list[tuple[Path, bool]] = []
        self.error_for = error_for

    def upload(self, path: Path, *, auto_tag: bool = True) -> IngestResultResponse:
        self.uploaded.append((path, auto_tag))
        if self.error_for and path.name == self.error_for:
            raise RequestRejected(f"Unsupported file type: {path.suffix}")
        return IngestResultResponse(
            file_path=str(path),
            document_id=f"doc-{path.stem}",
            status="completed",
            chunks_created=3,
            vectors_stored=3,
            tags_applied=1,
            error_message=None,
            duration_ms=800,
        )


def _make_tab(qtbot, client, max_bytes: int = 100 * 1024 * 1024):
    config = GuiConfig(
        base_url="http://testapi:8001", api_key="k", max_upload_bytes=max_bytes
    )
    tab = IngestTab(client, config, QThreadPool(), lambda _m: None)
    qtbot.addWidget(tab)
    return tab


def _statuses(tab) -> list[str]:
    return [
        tab.queue_table.item(row, 1).text() for row in range(tab.queue_table.rowCount())
    ]


class TestQueueFiltering:
    def test_unsupported_extension_is_rejected_without_upload(
        self, qtbot, tmp_path
    ) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        bad = tmp_path / "payload.exe"
        bad.write_bytes(b"MZ")

        tab.enqueue([bad])
        qtbot.waitUntil(lambda: bool(_statuses(tab)), timeout=3000)

        assert client.uploaded == []
        assert "unsupported" in _statuses(tab)[0].lower()

    def test_oversized_file_is_rejected_without_upload(self, qtbot, tmp_path) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client, max_bytes=4)
        big = tmp_path / "big.md"
        big.write_bytes(b"12345678")

        tab.enqueue([big])
        qtbot.waitUntil(lambda: bool(_statuses(tab)), timeout=3000)

        assert client.uploaded == []
        assert "too large" in _statuses(tab)[0].lower()

    def test_directory_is_rejected(self, qtbot, tmp_path) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)

        tab.enqueue([tmp_path])
        qtbot.waitUntil(lambda: bool(_statuses(tab)), timeout=3000)

        assert client.uploaded == []


class TestUploading:
    def test_uploads_supported_files_sequentially(self, qtbot, tmp_path) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        first = tmp_path / "a.md"
        second = tmp_path / "b.md"
        first.write_text("a")
        second.write_text("b")

        tab.enqueue([first, second])
        qtbot.waitUntil(lambda: len(client.uploaded) == 2, timeout=5000)
        qtbot.waitUntil(
            lambda: all("done" in s.lower() for s in _statuses(tab)), timeout=5000
        )

        assert [p.name for p, _ in client.uploaded] == ["a.md", "b.md"]
        assert tab.progress.value() == tab.progress.maximum()

    def test_passes_auto_tag_setting(self, qtbot, tmp_path) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        tab.auto_tag_checkbox.setChecked(False)
        sample = tmp_path / "a.md"
        sample.write_text("a")

        tab.enqueue([sample])
        qtbot.waitUntil(lambda: bool(client.uploaded), timeout=3000)

        assert client.uploaded[0][1] is False

    def test_emits_completion_signal_for_recent_tab(self, qtbot, tmp_path) -> None:
        client = _StubClient()
        tab = _make_tab(qtbot, client)
        sample = tmp_path / "a.md"
        sample.write_text("a")

        with qtbot.waitSignal(tab.ingest_completed, timeout=5000):
            tab.enqueue([sample])

    def test_failure_is_recorded_per_file_and_batch_continues(
        self, qtbot, tmp_path
    ) -> None:
        client = _StubClient(error_for="b.md")
        tab = _make_tab(qtbot, client)
        for name in ("a.md", "b.md", "c.md"):
            (tmp_path / name).write_text("x")

        tab.enqueue([tmp_path / "a.md", tmp_path / "b.md", tmp_path / "c.md"])
        qtbot.waitUntil(lambda: len(client.uploaded) == 3, timeout=6000)
        qtbot.waitUntil(
            lambda: all(
                any(word in s.lower() for word in ("done", "failed"))
                for s in _statuses(tab)
            ),
            timeout=6000,
        )

        statuses = _statuses(tab)
        assert "done" in statuses[0].lower()
        assert "failed" in statuses[1].lower()
        assert "done" in statuses[2].lower(), "one failure must not abort the batch"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_gui_ingest_tab.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Write the tab**

`grimoire/gui/widgets/ingest_tab.py`:

```python
"""Drag-and-drop ingest tab.

Files upload one at a time: the pipeline is CPU- and GPU-bound server side,
so parallel uploads would queue on the API anyway while making progress
meaningless.  Progress counts files, not bytes, because parsing and embedding
dominate the wall clock, not the local-network transfer.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from grimoire.api.schemas import IngestResultResponse
from grimoire.gui.config import SUPPORTED_EXTENSIONS, GuiConfig
from grimoire.gui.errors import GuiError
from grimoire.gui.workers import run_api_call

_COLUMNS = ("File", "Status", "Detail")


class DropZone(QFrame):
    """A frame that accepts dropped files and reports their paths."""

    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(110)
        label = QLabel("Drop files here to ingest, or use Browse")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.addWidget(label)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        """Accept the drag only when it carries local files."""
        mime = event.mimeData()
        if mime is not None and mime.hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """Emit the dropped local paths."""
        mime = event.mimeData()
        if mime is None:
            return
        paths = [
            Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()
        ]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()


class IngestTab(QWidget):
    """Queue dropped files and upload them one by one.

    Args:
        client: GrimoireClient (or a stub in tests).
        config: Supplies the client-side upload cap.
        pool: Shared thread pool.
        on_error: Callback that puts a message in the window's status bar.
    """

    ingest_completed = Signal()

    def __init__(
        self,
        client: Any,
        config: GuiConfig,
        pool: QThreadPool,
        on_error: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._client = client
        self._config = config
        self._pool = pool
        self._on_error = on_error
        self._pending: deque[tuple[int, Path]] = deque()
        self._uploading = False
        self._batch_total = 0
        self._batch_done = 0
        self._any_success = False

        self.drop_zone = DropZone()
        self.browse_button = QPushButton("Browse…")
        self.auto_tag_checkbox = QCheckBox("Auto-tag with the LLM")
        self.auto_tag_checkbox.setChecked(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)

        self.queue_table = QTableWidget(0, len(_COLUMNS))
        self.queue_table.setHorizontalHeaderLabels(list(_COLUMNS))
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        controls = QHBoxLayout()
        controls.addWidget(self.auto_tag_checkbox)
        controls.addStretch(1)
        controls.addWidget(self.browse_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.drop_zone)
        layout.addLayout(controls)
        layout.addWidget(self.progress)
        layout.addWidget(self.error_label)
        layout.addWidget(self.queue_table, stretch=1)

        self.drop_zone.files_dropped.connect(self.enqueue)
        self.browse_button.clicked.connect(self._browse)

    def set_client(self, client: Any) -> None:
        """Swap in a client rebuilt around a new session key."""
        self._client = client

    def _browse(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Select files to ingest")
        if names:
            self.enqueue([Path(name) for name in names])

    def enqueue(self, paths: list[Path]) -> None:
        """Add files to the queue, rejecting the ones the server would refuse.

        Rejected files stay visible with their reason: a file that vanishes
        from a drop is more confusing than one that explains itself.
        """
        self.error_label.clear()
        accepted = 0
        for path in paths:
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)
            self._set_row(row, path.name, "queued", "")
            reason = self._rejection_reason(path)
            if reason:
                self._set_row(row, path.name, "rejected", reason)
                continue
            self._pending.append((row, path))
            accepted += 1

        if accepted:
            self._batch_total += accepted
            self.progress.setRange(0, self._batch_total)
            self.progress.setValue(self._batch_done)
            self._start_next()

    def _rejection_reason(self, path: Path) -> str | None:
        """Why the server would refuse this file, or None if it would not."""
        if not path.is_file():
            return "Not a file (directories are not uploaded)"
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            return f"Unsupported file type: {suffix or '(no extension)'}"
        if path.stat().st_size > self._config.max_upload_bytes:
            limit_mb = self._config.max_upload_bytes / (1024 * 1024)
            return f"Too large (limit {limit_mb:.0f} MB)"
        return None

    def _start_next(self) -> None:
        """Upload the next queued file, if nothing is already in flight."""
        if self._uploading or not self._pending:
            return
        row, path = self._pending.popleft()
        self._uploading = True
        self._set_row(row, path.name, "uploading", "")
        auto_tag = self.auto_tag_checkbox.isChecked()
        run_api_call(
            self._pool,
            lambda: self._client.upload(path, auto_tag=auto_tag),
            lambda result: self._on_uploaded(row, path, result),
            lambda error: self._on_failed(row, path, error),
        )

    def _on_uploaded(
        self, row: int, path: Path, result: IngestResultResponse
    ) -> None:
        if result.status == "failed":
            self._set_row(
                row, path.name, "failed", result.error_message or "Ingestion failed"
            )
        elif result.status == "skipped":
            self._set_row(row, path.name, "skipped", "Already in the corpus")
            self._any_success = True
        else:
            self._set_row(
                row,
                path.name,
                "done",
                f"{result.chunks_created} chunks · {result.duration_ms} ms",
            )
            self._any_success = True
        self._finish_one()

    def _on_failed(self, row: int, path: Path, error: GuiError) -> None:
        self._set_row(row, path.name, "failed", error.message)
        self.error_label.setText(error.message)
        self._on_error(error.message)
        self._finish_one()

    def _finish_one(self) -> None:
        """Advance the batch: one failure never aborts the rest."""
        self._uploading = False
        self._batch_done += 1
        self.progress.setValue(self._batch_done)
        if self._pending:
            self._start_next()
            return
        if self._any_success:
            self.ingest_completed.emit()
        self._batch_total = 0
        self._batch_done = 0
        self._any_success = False
        self.progress.setRange(0, 1)
        self.progress.setValue(1)

    def _set_row(self, row: int, name: str, status: str, detail: str) -> None:
        for column, value in enumerate((name, status, detail)):
            self.queue_table.setItem(row, column, QTableWidgetItem(value))
```

- [ ] **Step 4: Register the tab and wire the refresh**

In `grimoire/gui/app.py`, after the Recent tab registration:

```python
        from grimoire.gui.widgets.ingest_tab import IngestTab

        self.ingest_tab = IngestTab(
            self.client, self.config, self.pool, self.show_error
        )
        self.tabs.addTab(self.ingest_tab, "Ingest")
        # A completed ingest is the one event worth refreshing the table for.
        self.ingest_tab.ingest_completed.connect(self.recent_tab.refresh)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_gui_ingest_tab.py -v
```

Expected: PASS, all seven tests. `test_uploads_supported_files_sequentially` also proves the queue drains rather than stalling after the first file.

- [ ] **Step 6: Check drag-and-drop by hand**

```bash
docker compose up -d api
GRIMOIRE_API_KEY=<a dev-tier key> uv run grimoire-gui
```

Drag a PDF from the host file manager onto the drop zone. Expected: it uploads, reports a chunk count, and the Recent ingests tab shows the new row. Drag a `.exe` — expected: rejected in the queue with a reason, no request made.

- [ ] **Step 7: Type-check and lint**

```bash
uv run mypy grimoire/gui/
uv run ruff check grimoire/gui/ tests/test_gui_ingest_tab.py
uv run black --check grimoire/gui/ tests/test_gui_ingest_tab.py
```

- [ ] **Step 8: Commit**

```bash
git add grimoire/gui/ tests/test_gui_ingest_tab.py
git commit -m "$(cat <<'EOF'
Add drag-and-drop ingest tab with per-file queue status

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: CLI runner tab

**Files:**
- Create: `grimoire/gui/widgets/cli_tab.py`
- Modify: `grimoire/gui/app.py` (register the tab)
- Test: `tests/test_gui_cli_tab.py`

**Interfaces:**
- Consumes: nothing from the client — this tab talks to a local process, not the API.
- Produces: `CliTab(QWidget)` — `__init__(self)`, methods `run() -> None`, `stop() -> None`, `shutdown() -> None`, `build_argv() -> list[str]`; attributes `.command_combo`, `.args_field`, `.output_view: QTextEdit`, `.run_button`, `.stop_button`; module constant `ALLOWED_COMMANDS: dict[str, list[str]]`

**Context:** No shell. The program is the `grimoire` executable (or `python -m grimoire.cli.main`), the subcommand comes from the dropdown and is never typed, and the arguments field is split with `shlex.split` into argv entries. Shell metacharacters therefore arrive at the child process as literal text rather than as syntax.

The allowlist is read-only commands. `keys` issues credentials, `migrate` mutates the schema, `reindex` is a long rewrite, `watch` starts a daemon outside the GUI's lifecycle, and `ingest` belongs to the Ingest tab. They remain available in a terminal, where their consequences are in front of the person running them.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gui_cli_tab.py`:

```python
"""Tests for the CLI runner tab."""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from grimoire.gui.widgets.cli_tab import ALLOWED_COMMANDS, CliTab  # noqa: E402


class TestAllowlist:
    def test_holds_only_read_only_commands(self) -> None:
        forbidden = {"keys", "migrate", "reindex", "untag", "watch", "ingest", "tag"}
        for argv in ALLOWED_COMMANDS.values():
            assert argv, "every entry needs at least a subcommand"
            assert argv[0] not in forbidden, f"{argv[0]} must not be runnable from the GUI"

    def test_includes_the_expected_commands(self) -> None:
        subcommands = {argv[0] for argv in ALLOWED_COMMANDS.values()}
        assert {"status", "search", "ask", "docs", "config", "cache"} <= subcommands


class TestArgvConstruction:
    def test_subcommand_comes_from_the_dropdown(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.command_combo.setCurrentText("status")
        tab.args_field.setText("--detailed")

        argv = tab.build_argv()

        assert argv[-2:] == ["status", "--detailed"]

    def test_shell_metacharacters_stay_literal(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.command_combo.setCurrentText("status")
        tab.args_field.setText("; rm -rf / && echo pwned")

        argv = tab.build_argv()

        assert "; rm -rf / && echo pwned" not in " ".join(argv[:1])
        assert ";" in argv, "the semicolon is an argument, not a separator"
        assert argv.count("status") == 1

    def test_typed_subcommand_cannot_replace_the_dropdown(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.command_combo.setCurrentText("status")
        tab.args_field.setText("keys create --tier agent")

        argv = tab.build_argv()

        assert argv.index("status") < argv.index("keys")

    def test_unbalanced_quotes_are_reported_not_raised(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.command_combo.setCurrentText("search")
        tab.args_field.setText("'unclosed")

        tab.run()

        assert "quote" in tab.output_view.toPlainText().lower()


class TestProcessLifecycle:
    def test_streams_output_and_reports_exit_code(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        # Run a known-good process instead of grimoire: this test is about
        # QProcess plumbing, not about the CLI.
        tab.start_process(sys.executable, ["-c", "print('hello from child')"])

        qtbot.waitUntil(
            lambda: "hello from child" in tab.output_view.toPlainText(), timeout=10000
        )
        qtbot.waitUntil(
            lambda: "exit code 0" in tab.output_view.toPlainText().lower(),
            timeout=10000,
        )
        assert tab.run_button.isEnabled()

    def test_nonzero_exit_is_surfaced(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.start_process(sys.executable, ["-c", "raise SystemExit(3)"])

        qtbot.waitUntil(
            lambda: "exit code 3" in tab.output_view.toPlainText().lower(),
            timeout=10000,
        )

    def test_stop_terminates_a_running_process(self, qtbot) -> None:
        tab = CliTab()
        qtbot.addWidget(tab)
        tab.start_process(
            sys.executable, ["-c", "import time; time.sleep(30)"]
        )
        qtbot.waitUntil(lambda: not tab.run_button.isEnabled(), timeout=5000)

        tab.stop()

        qtbot.waitUntil(lambda: tab.run_button.isEnabled(), timeout=10000)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_gui_cli_tab.py -v
```

Expected: FAIL — module not found.

- [ ] **Step 3: Write the tab**

`grimoire/gui/widgets/cli_tab.py`:

```python
"""CLI runner tab.

Runs a fixed set of read-only ``grimoire`` subcommands through QProcess and
streams their output.  There is no shell: the program is an executable path
and the arguments are an argv list, so nothing typed into the arguments field
can chain a second command.
"""

from __future__ import annotations

import shlex
import shutil
import sys
from typing import Any

from loguru import logger
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# Read-only commands only.  `keys` issues credentials, `migrate` mutates the
# schema, `reindex` is a long rewrite, `watch` starts a daemon outside this
# window's lifecycle, and `ingest` belongs to the Ingest tab.  All of them
# stay available in a terminal, where their consequences are visible.
ALLOWED_COMMANDS: dict[str, list[str]] = {
    "status": ["status"],
    "status --detailed": ["status", "--detailed"],
    "search": ["search"],
    "ask": ["ask"],
    "docs": ["docs"],
    "config show": ["config", "show"],
    "cache stats": ["cache", "stats"],
    "categories list": ["categories", "list"],
}

_MAX_OUTPUT_BLOCKS = 5000
_TERMINATE_GRACE_MS = 2000


class CliTab(QWidget):
    """Run an allowlisted grimoire command and watch its output."""

    def __init__(self) -> None:
        super().__init__()
        self._process: QProcess | None = None

        self.command_combo = QComboBox()
        self.command_combo.addItems(list(ALLOWED_COMMANDS))
        self.args_field = QLineEdit()
        self.args_field.setPlaceholderText("Extra arguments, e.g. --limit 5")
        self.run_button = QPushButton("Run")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)

        self.output_view = QTextEdit()
        self.output_view.setReadOnly(True)
        self.output_view.document().setMaximumBlockCount(_MAX_OUTPUT_BLOCKS)
        self.output_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

        controls = QHBoxLayout()
        controls.addWidget(self.command_combo)
        controls.addWidget(self.args_field, stretch=1)
        controls.addWidget(self.run_button)
        controls.addWidget(self.stop_button)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.output_view, stretch=1)

        self.run_button.clicked.connect(self.run)
        self.stop_button.clicked.connect(self.stop)

    # -- argv --------------------------------------------------------------

    @staticmethod
    def _program() -> tuple[str, list[str]]:
        """Locate the grimoire executable.

        Falls back to the running interpreter's module entry point, which is
        what a source checkout without an installed console script has.
        """
        executable = shutil.which("grimoire")
        if executable:
            return executable, []
        return sys.executable, ["-m", "grimoire.cli.main"]

    def build_argv(self) -> list[str]:
        """Assemble the full argv for the selected command.

        The subcommand always comes from the dropdown and is placed before
        anything the user typed, so a typed subcommand cannot displace it.
        """
        _, prefix = self._program()
        subcommand = ALLOWED_COMMANDS[self.command_combo.currentText()]
        extra = shlex.split(self.args_field.text())
        return [*prefix, *subcommand, *extra]

    # -- process lifecycle -------------------------------------------------

    def run(self) -> None:
        """Start the selected command."""
        if self._process is not None:
            return
        try:
            argv = self.build_argv()
        except ValueError as exc:
            # shlex raises on an unbalanced quote; that is user input, not a
            # bug, so it belongs in the output view.
            self._append(f"Could not parse arguments (check your quotes): {exc}")
            return
        program, _ = self._program()
        self.start_process(program, argv)

    def start_process(self, program: str, argv: list[str]) -> None:
        """Launch a process and stream its output.

        Args:
            program: Executable path.  Never a shell.
            argv: Arguments, already split into separate entries.
        """
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._drain_output)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_process_error)
        self._process = process

        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._append(f"$ {program} {' '.join(argv)}")
        process.start(program, argv)

    def stop(self) -> None:
        """Ask the process to exit, then kill it if it ignores the request."""
        process = self._process
        if process is None:
            return
        process.terminate()
        if not process.waitForFinished(_TERMINATE_GRACE_MS):
            self._append("Process ignored terminate; killing it.")
            process.kill()
            process.waitForFinished(_TERMINATE_GRACE_MS)

    def shutdown(self) -> None:
        """Called by the main window before the application closes."""
        self.stop()

    def _drain_output(self) -> None:
        process = self._process
        if process is None:
            return
        chunk = bytes(process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        if chunk:
            self._append(chunk.rstrip("\n"))

    def _on_finished(self, exit_code: int, _status: Any) -> None:
        self._drain_output()
        self._append(f"— finished with exit code {exit_code} —")
        self._process = None
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def _on_process_error(self, error: Any) -> None:
        logger.warning(f"CLI process error: {error}")
        if self._process is not None and self._process.state() == QProcess.NotRunning:
            self._append("Could not start the command. Is grimoire on your PATH?")
            self._process = None
            self.run_button.setEnabled(True)
            self.stop_button.setEnabled(False)

    def _append(self, text: str) -> None:
        self.output_view.append(text)
```

- [ ] **Step 4: Register the tab**

In `grimoire/gui/app.py`, after the Ingest tab registration:

```python
        from grimoire.gui.widgets.cli_tab import CliTab

        self.cli_tab = CliTab()
        self.tabs.addTab(self.cli_tab, "CLI")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/test_gui_cli_tab.py -v
```

Expected: PASS, all eight tests.

- [ ] **Step 6: Type-check and lint**

```bash
uv run mypy grimoire/gui/
uv run ruff check grimoire/gui/ tests/test_gui_cli_tab.py
uv run black --check grimoire/gui/ tests/test_gui_cli_tab.py
uv run bandit -r grimoire/gui/
```

Expected: clean. Bandit matters here specifically: it should find no `subprocess` call with `shell=True`, because there is none.

- [ ] **Step 7: Commit**

```bash
git add grimoire/gui/ tests/test_gui_cli_tab.py
git commit -m "$(cat <<'EOF'
Add CLI runner tab restricted to read-only grimoire commands

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Documentation and full verification

**Files:**
- Modify: `README.md` (a GUI section, plus the dev-install line)
- Modify: `CHANGELOG.md`
- Test: the whole suite

**Interfaces:**
- Consumes: everything above.
- Produces: a documented, verified feature.

- [ ] **Step 1: Document the GUI in the README**

Add a section after `### API` and before `### MCP (Model Context Protocol)`:

```markdown
### Desktop GUI

A PySide6 desktop client with four tabs: Search/Ask, Recent ingests,
drag-and-drop ingest, and a CLI runner.

```bash
# Install the optional GUI dependencies
uv sync --extra gui

# Point it at an API and launch
export GRIMOIRE_API_URL=http://localhost:8001   # default
export GRIMOIRE_API_KEY=grim_dvl_...            # required
grimoire-gui
```

The GUI is a thin HTTP client: it talks to the REST API and never imports the
ingestion pipeline, so it starts in a second and works identically against a
bare-metal API or the containerized stack. Drag-and-drop uses
`POST /api/v1/ingest/upload`, so the GUI and the API do **not** need to share a
filesystem.

An API key pasted into the connection bar is kept in memory for that session
only; nothing writes it to disk. Set `GRIMOIRE_API_KEY` in `.env` to avoid
retyping it.

**On WSL2 the GUI needs WSLg** (shipped with Windows 11). Check with
`echo "$DISPLAY $WAYLAND_DISPLAY"` — if both are empty, no window can open.

The CLI tab runs a fixed set of read-only commands (`status`, `search`, `ask`,
`docs`, `config show`, `cache stats`, `categories list`). Anything that issues
credentials, migrates the schema, or starts a daemon stays in a terminal by
design.
```

In `### Development`, add to the install commands:

```bash
# Install with the GUI extra as well
uv sync --extra dev --extra gui

# Run the GUI tests headless
QT_QPA_PLATFORM=offscreen uv run pytest tests/test_gui_*.py -v
```

- [ ] **Step 2: Update the changelog**

Add to `CHANGELOG.md` under the current unreleased heading (match the file's existing style):

```markdown
### Added
- Desktop GUI (`grimoire-gui`, `uv sync --extra gui`): Search/Ask with source
  chunk inspection, recent ingests, drag-and-drop ingest, and a read-only CLI
  runner.
- `POST /api/v1/ingest/upload` — multipart upload that stages the file server
  side, so ingestion no longer requires a shared filesystem.
- `GRIMOIRE_API__UPLOAD_DIR` and `GRIMOIRE_API__MAX_UPLOAD_BYTES` settings, with
  an `app_uploads` volume in `docker-compose.yml`.
```

- [ ] **Step 3: Run the full suite**

```bash
QT_QPA_PLATFORM=offscreen uv run pytest -v
```

Expected: everything passes, including the pre-existing tests. Read the full output rather than the summary line — a Qt test that errors during teardown can still be reported oddly.

- [ ] **Step 4: Run every quality gate**

```bash
uv run pre-commit run --all-files
uv run mypy grimoire/
uv run bandit -r grimoire/
```

Expected: clean. If `mypy` reports PySide6 stub problems that no narrow ignore can resolve, report them rather than widening the exclusion — the constraint is deliberate.

- [ ] **Step 5: End-to-end check against the containerized stack**

```bash
docker compose up -d
docker compose ps    # api healthy
GRIMOIRE_API_KEY=<a dev-tier key> uv run grimoire-gui
```

Walk all four tabs:
1. Ask a question; confirm the answer and its source cards.
2. Switch to Search; confirm it returns faster with no answer panel.
3. Drop a PDF from outside any mounted directory; confirm it ingests — this is the proof that the upload endpoint removed the shared-filesystem requirement.
4. Confirm the Recent tab picked up the new document.
5. Run `status --detailed` in the CLI tab; confirm output streams and the exit code prints.

Then confirm the upload survives a restart:

```bash
docker compose restart api
docker compose exec api ls /app/uploads
```

Expected: the uploaded file is still there.

- [ ] **Step 6: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "$(cat <<'EOF'
Document the desktop GUI and the upload endpoint

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Open the pull request**

```bash
git push -u origin plan-gui
gh pr create --base main --title "Add PySide6 desktop GUI" --body "$(cat <<'EOF'
Implements docs/superpowers/specs/2026-09-21-desktop-gui-design.md.

- `grimoire/gui/`: PySide6 client with Search/Ask, Recent ingests,
  drag-and-drop ingest, and a read-only CLI runner. Installed with
  `uv sync --extra gui`; the server image stays Qt-free.
- `POST /api/v1/ingest/upload`: multipart upload staged server side, so the
  GUI and the API no longer need a shared filesystem.
- `app_uploads` named volume, because the staging directory holds the only
  copy of every uploaded document.

Tests run headless with `QT_QPA_PLATFORM=offscreen`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Plan Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| WSLg prerequisite | Task 6 step 9, Task 11 README |
| Architecture / REST not MCP | Tasks 4–5 (client), rationale in the spec |
| Module layout | Tasks 4–10, one file per responsibility |
| Dependencies (`gui` extra, script, pytest-qt) | Task 4 |
| Concurrency (thread pool, no asyncio, timeouts, no stacking, shutdown) | Task 6 (`workers.py`, `closeEvent`), enforced per-tab in 7–10 |
| Search/Ask tab | Task 7 |
| Recent ingests tab | Task 8 |
| Drag-and-drop tab | Task 9 |
| CLI runner tab | Task 10 |
| Upload endpoint (415/413/sanitize/stream/retention) | Task 2 |
| Upload config fields | Task 1 |
| Named volume + docs | Task 3 |
| Configuration (env, in-memory key, unconfigured state) | Tasks 4 and 6 |
| Error handling table | Task 5 (`errors.py`, `_error_for`), `session_id` in Task 6 |
| Testing (client, widgets, endpoint, offscreen) | Tasks 2, 5–10; full suite in 11 |
| Risks | Addressed in the tasks they belong to |
| Definition of Done | Task 11 |

No gaps found.

**Type consistency check:** `GuiConfig`, `GrimoireClient` (`ask`/`search`/`recent_documents`/`upload`/`health`/`close`), `GuiError.message`/`.retry_after`, `run_api_call`, `set_client`, `shutdown`, and `ingest_completed` are used in later tasks exactly as Tasks 4–6 define them. `_staging_dir` and `_max_upload_bytes` are the two patch targets Task 2's tests use and the two helpers its implementation defines.

**Known rough edges the executor should expect:** `pytest-httpx` fixture semantics vary by version — Task 5 step 5 names the likely adjustment. Ruff's `BLE001` may not be in the selected rule set, in which case that one `noqa` is removed (Task 6 step 4 says so). Neither changes any behaviour.
