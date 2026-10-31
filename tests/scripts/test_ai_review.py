"""Tests for .github/scripts/review.py (the per-commit AI review workflow).

The network layer is replaced by a small fake that routes ``requests.get`` and
``requests.post`` by URL, so every path through ``main`` runs offline.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import requests

_SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "review.py"


def _load() -> ModuleType:
    # .github/scripts is not a package, so load the file directly.
    spec = importlib.util.spec_from_file_location("ai_review", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ai_review"] = module
    spec.loader.exec_module(module)
    return module


review = _load()

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
API = "https://api.github.com/repos/o/r"

ENV = {
    "REPO": "o/r",
    "PR_NUMBER": "7",
    "OLLAMA_MODEL": "glm-test",
    "GH_TOKEN": "gh-token",
    "OLLAMA_API_KEY": "ollama-key",
    "OLLAMA_HOST": "https://ollama.example",
}


def _commit(sha: str, msg: str = "Do a thing", parents: int = 1) -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {"message": msg},
        "parents": [{"sha": "p" * 40}] * parents,
    }


def _bot_comment(body: str, login: str = review.BOT_LOGIN) -> dict[str, Any]:
    return {"user": {"login": login}, "body": body}


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        json_data: Any = None,
        text: str = "",
        next_url: str | None = None,
    ) -> None:
        self.status_code = status
        self._json = json_data
        self.text = text
        self.links = {"next": {"url": next_url}} if next_url else {}

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeHTTP:
    """Routes calls by URL and records them for assertions."""

    def __init__(self) -> None:
        self.get_routes: dict[str, FakeResponse] = {}
        self.post_routes: dict[str, Callable[[dict[str, Any]], FakeResponse]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self.get_routes[url]

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self.post_routes[url](kwargs)

    def posted_comments(self) -> list[str]:
        return [
            kw["json"]["body"]
            for method, url, kw in self.calls
            if method == "POST" and url.endswith("/issues/7/comments")
        ]


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> FakeHTTP:
    fake = FakeHTTP()
    monkeypatch.setattr(review.requests, "get", fake.get)
    monkeypatch.setattr(review.requests, "post", fake.post)
    return fake


def _wire_pr(
    http: FakeHTTP,
    commits: list[dict[str, Any]],
    comments: list[dict[str, Any]] | None = None,
    reply: str = "- looks fine",
) -> None:
    """Route the PR listing, per-commit diffs, Ollama and comment posting."""
    http.get_routes[f"{API}/pulls/7/commits?per_page=100"] = FakeResponse(
        json_data=commits
    )
    http.get_routes[f"{API}/issues/7/comments?per_page=100"] = FakeResponse(
        json_data=comments or []
    )
    for c in commits:
        http.get_routes[f"{API}/commits/{c['sha']}"] = FakeResponse(
            text=f"diff for {c['sha'][:7]}"
        )
    http.post_routes["https://ollama.example/api/chat"] = lambda kw: FakeResponse(
        json_data={"message": {"content": reply}}
    )
    http.post_routes[f"{API}/issues/7/comments"] = lambda kw: FakeResponse(201)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_builds_headers_and_api(self) -> None:
        cfg = review.load_config(ENV)
        assert cfg.api == API
        assert cfg.gh_headers["Authorization"] == "Bearer gh-token"
        assert cfg.ollama_headers == {"Authorization": "Bearer ollama-key"}
        assert cfg.host == "https://ollama.example"

    def test_host_defaults_to_ollama_cloud(self) -> None:
        env = {k: v for k, v in ENV.items() if k != "OLLAMA_HOST"}
        assert review.load_config(env).host == "https://ollama.com"

    @pytest.mark.parametrize("name", review.REQUIRED_ENV)
    def test_empty_required_value_exits_with_its_name(self, name: str) -> None:
        # Unset GitHub secrets arrive as "" rather than missing.
        with pytest.raises(SystemExit, match=name):
            review.load_config({**ENV, name: ""})

    def test_lists_every_missing_name(self) -> None:
        with pytest.raises(SystemExit) as exc:
            review.load_config({})
        for name in review.REQUIRED_ENV:
            assert name in str(exc.value)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestSanitize:
    def test_strips_smuggled_marker(self) -> None:
        out = review.sanitize(f"{review.marker(SHA_A)}\nreal text")
        assert "ai-review" not in out
        assert out == "real text"

    def test_strips_multiline_comment(self) -> None:
        assert review.sanitize("a<!--\nhidden\n-->b") == "ab"

    def test_strips_dangling_opener(self) -> None:
        assert review.sanitize("visible <!-- rest") == "visible  rest"

    def test_whitespace_only_becomes_empty(self) -> None:
        assert review.sanitize("  <!-- x -->  ") == ""


class TestReviewedShas:
    def test_trusts_only_bot_marker_at_start(self) -> None:
        comments = [
            _bot_comment(f"{review.marker(SHA_A)}\nreview"),
            # Same marker, but from a human: could be forged.
            _bot_comment(f"{review.marker(SHA_B)}\nfake", login="someone"),
            # Bot comment, but marker not at the very start.
            _bot_comment(f"quoted: {review.marker(SHA_C)}"),
        ]
        assert review.reviewed_shas(comments) == {SHA_A}

    def test_tolerates_missing_user_and_body(self) -> None:
        assert review.reviewed_shas([{"user": None, "body": None}, {}]) == set()

    def test_rejects_short_sha(self) -> None:
        assert (
            review.reviewed_shas([_bot_comment("<!-- ai-review:abc123 -->")]) == set()
        )


class TestSelectTodo:
    def test_skips_done_and_merge_commits(self) -> None:
        commits = [_commit(SHA_A), _commit(SHA_B, parents=2), _commit(SHA_C)]
        todo = review.select_todo(commits, done={SHA_A})
        assert [c["sha"] for c in todo] == [SHA_C]

    def test_caps_to_newest(self, capsys: pytest.CaptureFixture[str]) -> None:
        commits = [_commit(f"{i:040x}") for i in range(5)]
        todo = review.select_todo(commits, done=set(), max_commits=2)
        assert [c["sha"] for c in todo] == [f"{3:040x}", f"{4:040x}"]
        assert "reviewing the newest 2" in capsys.readouterr().out

    def test_root_commit_with_no_parents_is_reviewed(self) -> None:
        assert len(review.select_todo([_commit(SHA_A, parents=0)], done=set())) == 1


class TestSmallHelpers:
    @pytest.mark.parametrize(
        ("msg", "expected"),
        [
            ("Fix bug\n\nbody", "Fix bug"),
            ("\n\n  Title  \n", "Title"),
            ("", "(no message)"),
        ],
    )
    def test_first_line(self, msg: str, expected: str) -> None:
        assert review.first_line(msg) == expected

    def test_truncate(self) -> None:
        assert review.truncate("abcdef", limit=4) == ("abcd", True)
        assert review.truncate("abc", limit=3) == ("abc", False)

    def test_prompt_contains_message_diff_and_untrusted_warning(self) -> None:
        prompt = review.build_prompt("msg here", "+diff here")
        assert "msg here" in prompt and "+diff here" in prompt
        assert "untrusted data" in prompt

    def test_body_starts_with_marker(self) -> None:
        body = review.build_body(SHA_A, "m", "Title\nmore", "- ok", truncated=False)
        assert review.MARKER_RE.match(body).group(1) == SHA_A  # type: ignore[union-attr]
        assert "`aaaaaaa`" in body and "_Title_" in body
        assert "truncated" not in body

    def test_body_notes_truncation(self) -> None:
        body = review.build_body(SHA_A, "m", "t", "r", truncated=True, limit=10)
        assert "truncated to 10 chars" in body


# ---------------------------------------------------------------------------
# Network functions
# ---------------------------------------------------------------------------


class TestGetAll:
    def test_follows_link_pagination(self, http: FakeHTTP) -> None:
        http.get_routes["u1"] = FakeResponse(json_data=[1, 2], next_url="u2")
        http.get_routes["u2"] = FakeResponse(json_data=[3])
        assert review.get_all("u1", {}) == [1, 2, 3]

    def test_raises_on_http_error(self, http: FakeHTTP) -> None:
        http.get_routes["u1"] = FakeResponse(status=403)
        with pytest.raises(requests.HTTPError):
            review.get_all("u1", {})


class TestReviewCommit:
    def test_posts_sanitized_review_with_marker(self, http: FakeHTTP) -> None:
        _wire_pr(
            http, [_commit(SHA_A)], reply=f"{review.marker(SHA_B)}\n- bug on line 3"
        )
        review.review_commit(review.load_config(ENV), _commit(SHA_A))

        [body] = http.posted_comments()
        assert body.startswith(review.marker(SHA_A))
        assert SHA_B not in body  # the model's forged marker was stripped
        assert "- bug on line 3" in body

        chat = next(kw for m, url, kw in http.calls if url.endswith("/api/chat"))
        assert chat["headers"] == {"Authorization": "Bearer ollama-key"}
        assert chat["json"]["model"] == "glm-test"
        assert "diff for aaaaaaa" in chat["json"]["messages"][0]["content"]

    def test_requests_diff_media_type(self, http: FakeHTTP) -> None:
        _wire_pr(http, [_commit(SHA_A)])
        review.review_commit(review.load_config(ENV), _commit(SHA_A))
        diff_call = next(
            kw for m, url, kw in http.calls if url.endswith(f"/commits/{SHA_A}")
        )
        assert diff_call["headers"]["Accept"] == "application/vnd.github.diff"

    def test_diff_error_never_reaches_model(self, http: FakeHTTP) -> None:
        _wire_pr(http, [_commit(SHA_A)])
        http.get_routes[f"{API}/commits/{SHA_A}"] = FakeResponse(
            status=404, text="Not Found"
        )
        with pytest.raises(requests.HTTPError):
            review.review_commit(review.load_config(ENV), _commit(SHA_A))
        assert not any(url.endswith("/api/chat") for _, url, _ in http.calls)

    def test_empty_review_raises_and_posts_nothing(self, http: FakeHTTP) -> None:
        _wire_pr(http, [_commit(SHA_A)], reply="  <!-- only a comment -->  ")
        with pytest.raises(ValueError, match="empty review"):
            review.review_commit(review.load_config(ENV), _commit(SHA_A))
        assert http.posted_comments() == []

    def test_long_diff_is_truncated_and_flagged(self, http: FakeHTTP) -> None:
        _wire_pr(http, [_commit(SHA_A)])
        http.get_routes[f"{API}/commits/{SHA_A}"] = FakeResponse(
            text="x" * (review.MAX_DIFF + 5)
        )
        review.review_commit(review.load_config(ENV), _commit(SHA_A))

        chat = next(kw for m, url, kw in http.calls if url.endswith("/api/chat"))
        prompt = chat["json"]["messages"][0]["content"]
        assert "x" * review.MAX_DIFF in prompt
        assert "x" * (review.MAX_DIFF + 1) not in prompt
        [body] = http.posted_comments()
        assert "the tail was not reviewed" in body


class TestWriteSummary:
    def test_appends_failures(self, tmp_path: Path) -> None:
        path = tmp_path / "summary.md"
        path.write_text("existing\n")
        review.write_summary(str(path), [("aaaaaaa", "boom")])
        assert (
            path.read_text() == "existing\n### AI review failures\n- `aaaaaaa`: boom\n"
        )

    def test_no_path_is_a_no_op(self) -> None:
        review.write_summary(None, [("aaaaaaa", "boom")])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    def test_reviews_only_new_non_merge_commits(self, http: FakeHTTP) -> None:
        commits = [_commit(SHA_A), _commit(SHA_B, parents=2), _commit(SHA_C)]
        done = [_bot_comment(f"{review.marker(SHA_A)}\nold review")]
        _wire_pr(http, commits, comments=done)

        assert review.main(ENV) == 0
        bodies = http.posted_comments()
        assert len(bodies) == 1 and bodies[0].startswith(review.marker(SHA_C))

    def test_nothing_to_do(self, http: FakeHTTP) -> None:
        _wire_pr(http, [_commit(SHA_A)], comments=[_bot_comment(review.marker(SHA_A))])
        assert review.main(ENV) == 0
        assert http.posted_comments() == []

    def test_one_failure_does_not_stop_the_rest(
        self, http: FakeHTTP, tmp_path: Path
    ) -> None:
        _wire_pr(http, [_commit(SHA_A), _commit(SHA_B)])
        http.get_routes[f"{API}/commits/{SHA_A}"] = FakeResponse(status=500)
        summary = tmp_path / "summary.md"

        assert review.main({**ENV, "GITHUB_STEP_SUMMARY": str(summary)}) == 1
        bodies = http.posted_comments()
        assert len(bodies) == 1 and bodies[0].startswith(review.marker(SHA_B))
        assert "`aaaaaaa`: HTTP 500" in summary.read_text()

    def test_missing_secret_stops_before_any_request(self, http: FakeHTTP) -> None:
        with pytest.raises(SystemExit, match="OLLAMA_API_KEY"):
            review.main({**ENV, "OLLAMA_API_KEY": ""})
        assert http.calls == []
