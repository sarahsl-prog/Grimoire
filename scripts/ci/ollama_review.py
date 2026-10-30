"""Ask an Ollama model to review a pull request diff.

Run by ``.github/workflows/ollama-review.yml``. Reads a unified diff, asks the
model for a review against Grimoire's code standards, and writes Markdown that
the workflow posts as a sticky PR comment.

The review is advisory: every failure path (Ollama unreachable, model missing,
empty reply) still writes a comment explaining what went wrong and exits 0, so
a flaky model never turns a PR red. Lint, types and tests are the real gates.

Only the standard library is used, so the workflow can run this with a bare
Python and skip installing Grimoire's (large) dependency tree.

Usage:
    python scripts/ci/ollama_review.py --diff pr.diff --out review.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# Marker the workflow uses to find and update its own comment instead of
# posting a new one on every push.
COMMENT_MARKER = "<!-- ollama-review -->"

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b"
# ~4 chars/token: 48K chars keeps diff + prompt + reply inside a 16K context,
# which is what a CPU runner can process in a few minutes.
DEFAULT_MAX_DIFF_CHARS = 48_000
DEFAULT_NUM_CTX = 16_384
# CPU inference on a hosted runner is slow; generous, but bounded.
DEFAULT_TIMEOUT_S = 900

# Files whose diffs are noise to a reviewer and burn the context budget.
SKIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)uv\.lock$"),
    re.compile(r"(^|/)package-lock\.json$"),
    re.compile(r"\.(png|jpe?g|gif|ico|pdf|svg|woff2?)$"),
    re.compile(r"^alembic/versions/"),
)

SYSTEM_PROMPT = """\
You are a senior Python reviewer for Grimoire, a knowledge-management system \
(Python 3.13, FastAPI, SQLAlchemy async, PostgreSQL, ChromaDB, Ollama) with a \
security-data corpus (CVE, Sigma, MITRE ATT&CK, IR playbooks).

Review ONLY the diff you are given. Project standards:
- Full mypy-strict type annotations on every function signature.
- loguru logging on major features; never surface raw exceptions to users or \
to the LLM.
- Vigorous validation of user input and external API responses.
- pytest tests accompany new behaviour.
- Comments explain *why*, not *what*.

Report, most severe first:
1. Bugs and correctness problems (wrong logic, unhandled None, async misuse).
2. Security issues (injection, path traversal, secrets, unsafe deserialisation).
3. Missing tests or validation for new behaviour.
4. Clear violations of the standards above.

Rules: cite file and line (from the diff hunk headers) for every finding. Do \
not invent code that is not in the diff. Skip pure style that ruff/black \
already enforce. If you find nothing significant, say so in one line. Be \
concise: a bullet list grouped under the four headings, no preamble."""


@dataclass
class PreparedDiff:
    """A diff trimmed to fit the model's context window.

    Attributes:
        text: Diff text to send to the model.
        skipped_files: Files dropped as noise (lockfiles, binaries, migrations).
        truncated: True when the diff was cut to fit ``max_chars``.
    """

    text: str
    skipped_files: list[str] = field(default_factory=list)
    truncated: bool = False


def _file_of(section: str) -> str:
    """Return the post-change path from a ``diff --git a/x b/y`` section."""
    first_line = section.split("\n", 1)[0]
    match = re.match(r"diff --git a/(\S+) b/(\S+)", first_line)
    return match.group(2) if match else ""


def prepare_diff(diff: str, max_chars: int) -> PreparedDiff:
    """Drop noisy files and truncate the diff on a file boundary.

    Truncating mid-hunk would show the model half a function and invite
    invented findings, so whole file sections are kept or dropped.

    Args:
        diff: Unified diff as produced by ``git diff``.
        max_chars: Character budget for the returned diff text.

    Returns:
        The prepared diff with bookkeeping about what was left out.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    # Split before each "diff --git" header, keeping the header with its body.
    sections = [s for s in re.split(r"(?m)^(?=diff --git )", diff) if s.strip()]

    kept: list[str] = []
    skipped: list[str] = []
    used = 0
    truncated = False
    for section in sections:
        path = _file_of(section)
        if any(p.search(path) for p in SKIP_PATTERNS):
            skipped.append(path)
            continue
        if used + len(section) > max_chars:
            truncated = True
            # A single file larger than the whole budget still gets reviewed,
            # cut down, rather than leaving the model with nothing.
            if not kept:
                kept.append(section[:max_chars])
            continue
        kept.append(section)
        used += len(section)

    return PreparedDiff(text="".join(kept), skipped_files=skipped, truncated=truncated)


def _validate_host(host: str) -> str:
    """Accept only http(s) URLs, so ``urlopen`` cannot be pointed at file://."""
    parsed = urlparse(host)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"OLLAMA_HOST must be an http(s) URL, got {host!r}")
    return host.rstrip("/")


def request_review(
    diff_text: str,
    *,
    host: str,
    model: str,
    num_ctx: int = DEFAULT_NUM_CTX,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> str:
    """Send the diff to Ollama's chat endpoint and return the reply text.

    Args:
        diff_text: Prepared diff to review.
        host: Base URL of the Ollama server.
        model: Ollama model tag.
        num_ctx: Context window to request.
        timeout_s: Socket timeout for the whole request.

    Returns:
        The model's review, stripped.

    Raises:
        RuntimeError: When Ollama is unreachable or returns an unusable reply.
    """
    url = f"{_validate_host(host)}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Review this diff:\n\n```diff\n{diff_text}\n```",
            },
        ],
    }
    req = urllib.request.Request(  # noqa: S310 - scheme checked in _validate_host
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # Scheme checked in _validate_host.
        opened = urllib.request.urlopen(  # noqa: S310  # nosec B310
            req, timeout=timeout_s
        )
        with opened as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Ollama returned HTTP {exc.code} for model {model!r}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"could not reach Ollama at {host}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Ollama returned a non-JSON response") from exc

    # Validate the shape rather than trusting it: an error payload or a
    # proxy page must not end up posted as "the review".
    message = body.get("message") if isinstance(body, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Ollama returned an empty review")
    return content.strip()


def render_comment(
    review: str | None,
    *,
    model: str,
    prepared: PreparedDiff | None,
    error: str | None = None,
) -> str:
    """Build the Markdown body for the PR comment.

    Args:
        review: The model's review, or None when it failed.
        model: Model tag, shown so readers can weigh the review.
        prepared: The diff that was reviewed, for the coverage footnote.
        error: Failure reason when ``review`` is None.

    Returns:
        Markdown starting with :data:`COMMENT_MARKER`.
    """
    lines = [COMMENT_MARKER, f"### 🦙 Ollama review (`{model}`)", ""]
    if review is not None:
        lines.append(review)
    else:
        lines.append(f"_Review unavailable: {error or 'unknown error'}._")
    lines.append("")

    notes: list[str] = []
    if prepared is not None:
        if prepared.truncated:
            notes.append("the diff was truncated to fit the model's context")
        if prepared.skipped_files:
            shown = ", ".join(f"`{f}`" for f in prepared.skipped_files[:5])
            more = len(prepared.skipped_files) - 5
            notes.append(f"skipped {shown}" + (f" and {more} more" if more > 0 else ""))
    notes.append("advisory only; this check never blocks merging")
    lines.append("<sub>" + "; ".join(notes).capitalize() + ".</sub>")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Always returns 0; see the module docstring."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--diff", type=Path, required=True, help="unified diff file")
    parser.add_argument("--out", type=Path, required=True, help="Markdown output")
    args = parser.parse_args(argv)

    host = os.environ.get("OLLAMA_HOST") or DEFAULT_HOST
    model = os.environ.get("OLLAMA_REVIEW_MODEL") or DEFAULT_MODEL
    try:
        max_chars = int(
            os.environ.get("OLLAMA_REVIEW_MAX_DIFF_CHARS") or DEFAULT_MAX_DIFF_CHARS
        )
    except ValueError:
        max_chars = DEFAULT_MAX_DIFF_CHARS

    prepared: PreparedDiff | None = None
    try:
        diff = args.diff.read_text(encoding="utf-8", errors="replace")
        prepared = prepare_diff(diff, max_chars)
        if not prepared.text.strip():
            body = render_comment(
                "Nothing to review: the diff only touches skipped files.",
                model=model,
                prepared=prepared,
            )
        else:
            review = request_review(prepared.text, host=host, model=model)
            body = render_comment(review, model=model, prepared=prepared)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ollama review failed: {exc}", file=sys.stderr)
        body = render_comment(None, model=model, prepared=prepared, error=str(exc))

    args.out.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
