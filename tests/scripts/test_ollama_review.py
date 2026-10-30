"""Tests for scripts/ci/ollama_review.py (the PR review workflow's helper)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "ollama_review.py"


def _load() -> ModuleType:
    # scripts/ is not a package, so load the file directly.
    spec = importlib.util.spec_from_file_location("ollama_review", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ollama_review"] = module
    spec.loader.exec_module(module)
    return module


review = _load()


def _section(path: str, body: str = "+x = 1\n") -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _fake_urlopen(payload: Any) -> Any:
    def opener(req: Any, timeout: int) -> _FakeResponse:
        opener.request = req  # type: ignore[attr-defined]
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return _FakeResponse(raw)

    return opener


# ---------------------------------------------------------------------------
# prepare_diff
# ---------------------------------------------------------------------------


class TestPrepareDiff:
    def test_keeps_code_and_skips_lockfiles_and_migrations(self) -> None:
        diff = (
            _section("grimoire/a.py")
            + _section("uv.lock")
            + _section("alembic/versions/001_x.py")
        )
        prepared = review.prepare_diff(diff, 10_000)
        assert "grimoire/a.py" in prepared.text
        assert "uv.lock" not in prepared.text
        assert prepared.skipped_files == ["uv.lock", "alembic/versions/001_x.py"]
        assert prepared.truncated is False

    def test_truncates_on_file_boundary(self) -> None:
        first = _section("grimoire/a.py", "+" + "a" * 100 + "\n")
        second = _section("grimoire/b.py", "+" + "b" * 100 + "\n")
        prepared = review.prepare_diff(first + second, len(first) + 10)
        assert prepared.text == first
        assert prepared.truncated is True

    def test_oversized_single_file_is_cut_not_dropped(self) -> None:
        big = _section("grimoire/a.py", "+" + "a" * 500 + "\n")
        prepared = review.prepare_diff(big, 100)
        assert len(prepared.text) == 100
        assert prepared.truncated is True

    def test_empty_diff(self) -> None:
        prepared = review.prepare_diff("", 100)
        assert prepared.text == ""
        assert prepared.skipped_files == []

    def test_rejects_non_positive_budget(self) -> None:
        with pytest.raises(ValueError):
            review.prepare_diff(_section("a.py"), 0)


# ---------------------------------------------------------------------------
# request_review
# ---------------------------------------------------------------------------


class TestRequestReview:
    def test_returns_stripped_content_and_sends_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opener = _fake_urlopen({"message": {"content": "  - looks fine  \n"}})
        monkeypatch.setattr(review.urllib.request, "urlopen", opener)

        out = review.request_review("diff", host="http://ollama:11434/", model="m:1")

        assert out == "- looks fine"
        assert opener.request.full_url == "http://ollama:11434/api/chat"
        sent = json.loads(opener.request.data)
        assert sent["model"] == "m:1"
        assert sent["stream"] is False

    @pytest.mark.parametrize("host", ["file:///etc/passwd", "ollama:11434", ""])
    def test_rejects_non_http_hosts(self, host: str) -> None:
        with pytest.raises(ValueError):
            review.request_review("diff", host=host, model="m")

    @pytest.mark.parametrize(
        "payload",
        [
            {"error": "model not found"},
            {"message": {"content": "   "}},
            ["x"],
            b"<html>",
        ],
    )
    def test_unusable_replies_raise(
        self, monkeypatch: pytest.MonkeyPatch, payload: Any
    ) -> None:
        monkeypatch.setattr(review.urllib.request, "urlopen", _fake_urlopen(payload))
        with pytest.raises(RuntimeError):
            review.request_review("diff", host="http://h", model="m")

    def test_connection_error_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(req: Any, timeout: int) -> None:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(review.urllib.request, "urlopen", refuse)
        with pytest.raises(RuntimeError, match="could not reach Ollama"):
            review.request_review("diff", host="http://h", model="m")


# ---------------------------------------------------------------------------
# render_comment / main
# ---------------------------------------------------------------------------


class TestRenderComment:
    def test_success_has_marker_model_and_notes(self) -> None:
        prepared = review.PreparedDiff(
            text="x", skipped_files=[f"f{i}" for i in range(7)], truncated=True
        )
        body = review.render_comment("- bug", model="m:1", prepared=prepared)
        assert body.startswith(review.COMMENT_MARKER)
        assert "`m:1`" in body
        assert "- bug" in body
        assert "truncated" in body
        assert "and 2 more" in body
        assert "never blocks merging" in body

    def test_failure_shows_error(self) -> None:
        body = review.render_comment(None, model="m", prepared=None, error="boom")
        assert "Review unavailable: boom" in body


class TestMain:
    def test_always_exits_zero_when_ollama_is_down(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def refuse(req: Any, timeout: int) -> None:
            raise urllib.error.URLError("down")

        monkeypatch.setattr(review.urllib.request, "urlopen", refuse)
        monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")
        diff = tmp_path / "pr.diff"
        diff.write_text(_section("grimoire/a.py"))
        out = tmp_path / "review.md"

        assert review.main(["--diff", str(diff), "--out", str(out)]) == 0
        assert "Review unavailable" in out.read_text()

    def test_only_skipped_files_skips_the_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def explode(req: Any, timeout: int) -> None:
            raise AssertionError("model should not be called")

        monkeypatch.setattr(review.urllib.request, "urlopen", explode)
        diff = tmp_path / "pr.diff"
        diff.write_text(_section("uv.lock"))
        out = tmp_path / "review.md"

        assert review.main(["--diff", str(diff), "--out", str(out)]) == 0
        assert "Nothing to review" in out.read_text()

    def test_bad_max_chars_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            review.urllib.request,
            "urlopen",
            _fake_urlopen({"message": {"content": "ok"}}),
        )
        monkeypatch.setenv("OLLAMA_REVIEW_MAX_DIFF_CHARS", "lots")
        diff = tmp_path / "pr.diff"
        diff.write_text(_section("grimoire/a.py"))
        out = tmp_path / "review.md"

        assert review.main(["--diff", str(diff), "--out", str(out)]) == 0
        assert "ok" in out.read_text()
