"""Pure helpers that turn API data into text for the terminal UI.

Everything here is a plain function over plain values: no Textual, no I/O, no
event loop.  That is deliberate.  The widgets stay thin, and the fiddly cases
(unit boundaries, garbage timestamps, odd tag input) are tested directly.

Do not import the pipeline from here.  The choice lists below are copies of
enums that live in modules which pull in SQLAlchemy and friends; tests assert
they have not drifted from the real enums.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from grimoire.api.schemas import QueryResponse, SearchResponse

# Mirrors the ``top_k`` bounds on QueryRequest / SearchRequest (ge=1, le=100);
# the API answers anything outside them with a 422.
TOP_K_MIN = 1
TOP_K_MAX = 100

# Dropdown choices.  Mirrors of grimoire.strategies.security.corpus.SourceType,
# grimoire.strategies.security.metadata.Severity, and the ProcessingStatus and
# FileType enums in grimoire.db.models.
SOURCE_TYPES: tuple[str, ...] = (
    "nvd_cve",
    "sigma_rule",
    "mitre_attack",
    "ioc_list",
    "playbook",
    "prose",
    "unknown",
)
SEVERITIES: tuple[str, ...] = (
    "critical",
    "high",
    "medium",
    "low",
    "info",
    "unknown",
)
DOC_STATUSES: tuple[str, ...] = (
    "pending",
    "processing",
    "completed",
    "failed",
    "stale",
)
FILE_TYPES: tuple[str, ...] = (
    "pdf",
    "docx",
    "pptx",
    "xlsx",
    "html",
    "md",
    "txt",
    "image",
    "audio",
    "video",
    "other",
)

_UNTITLED = "(untitled)"
_ELLIPSIS = "…"
_SIZE_UNITS = ("B", "KB", "MB", "GB")


@dataclass(frozen=True)
class SourceView:
    """One retrieved chunk, in the shape the source list and preview need.

    Ask responses (citations) and Search responses (results) carry slightly
    different fields; this is the common denominator so the widget has one
    rendering path.

    Attributes:
        title: Document title, or ``"(untitled)"``.
        text: Full chunk text.  Never truncated here; the preview shows it
            all and only the list row is shortened.
        score: Relevance score, or None when the server sent a non-finite
            number (which would otherwise render as ``nan``).
        document_id: Owning document, or ``""`` when the server sent none.
        chunk_ref: Short human label for the chunk (``"chunk 3"``), or the
            raw chunk id when there is no index, or ``""``.
    """

    title: str
    text: str
    score: float | None
    document_id: str
    chunk_ref: str


def _title(raw: str | None) -> str:
    cleaned = (raw or "").strip()
    return cleaned or _UNTITLED


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def sources_from_query(response: QueryResponse) -> list[SourceView]:
    """Adapt an Ask response's citations, preserving the server's order."""
    return [
        SourceView(
            title=_title(c.document_title),
            text=c.content_snippet,
            score=_finite(c.relevance_score),
            document_id=c.document_id,
            # `is not None`, not truthiness: index 0 is a real chunk.
            chunk_ref=(
                f"chunk {c.chunk_index}" if c.chunk_index is not None else c.chunk_id
            ),
        )
        for c in response.citations
    ]


def sources_from_search(response: SearchResponse) -> list[SourceView]:
    """Adapt a Search response's results, preserving the server's order."""
    return [
        SourceView(
            title=_title(r.document_title),
            text=r.content,
            score=_finite(r.score),
            document_id=r.document_id,
            chunk_ref=r.chunk_id,
        )
        for r in response.results
    ]


def format_size(size_bytes: int) -> str:
    """Human-readable size using 1024-based units, capped at GB.

    Negative sizes are nonsense from the server and render as ``"-"``.
    """
    if size_bytes < 0:
        return "-"
    if size_bytes < 1024:
        return f"{size_bytes} B"

    value = float(size_bytes)
    unit = 0
    while value >= 1024 and unit < len(_SIZE_UNITS) - 1:
        value /= 1024
        unit += 1
    # 1048575 B is 1023.999 KB, which would print as "1024.0 KB".  Promote it.
    if round(value, 1) >= 1024 and unit < len(_SIZE_UNITS) - 1:
        value /= 1024
        unit += 1
    return f"{value:.1f} {_SIZE_UNITS[unit]}"


def format_timestamp(iso: str | None) -> str:
    """``YYYY-MM-DD HH:MM`` from an ISO-8601 string, or ``"-"``.

    The wall-clock time the server sent is kept as is; there is no timezone
    conversion, so a table never silently disagrees with the API.  Anything
    unparseable renders as ``"-"`` rather than being echoed, since the value
    came over the network.
    """
    if iso is None or not iso.strip():
        return "-"
    try:
        parsed = datetime.fromisoformat(iso.strip())
    except ValueError:
        return "-"
    return parsed.strftime("%Y-%m-%d %H:%M")


def truncate(text: str, width: int) -> str:
    """Collapse ``text`` to one line and cut it to at most ``width`` characters.

    Whitespace runs (including newlines) become single spaces so a table cell
    is always one line.  A cut ends in a single ellipsis character that counts
    toward ``width``.  A non-positive width yields ``""``.
    """
    if width <= 0:
        return ""
    single_line = " ".join(text.split())
    if len(single_line) <= width:
        return single_line
    return single_line[: width - 1] + _ELLIPSIS


def build_filter_dict(
    *,
    tags: str,
    source_type: str | None,
    severity: str | None,
    cve_id: str,
) -> dict[str, Any] | None:
    """Compose the ``filter_dict`` the query endpoints accept.

    A small re-implementation of ``grimoire.cli.query._build_filter_dict``
    rather than an import of it: that module pulls in the settings and
    database layers.  A test asserts the two agree.

    Args:
        tags: Comma-separated tags as typed.  Blank items are dropped.
        source_type: A value from ``SOURCE_TYPES``, or None / ``""`` for any.
        severity: A value from ``SEVERITIES``, or None / ``""`` for any.
        cve_id: CVE id as typed, or blank.

    Returns:
        The filter dict, or None when nothing is set so the client omits the
        key entirely.
    """
    filters: dict[str, Any] = {}
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if tag_list:
        filters["tags"] = tag_list
    if severity:
        filters["severity"] = severity
    if source_type:
        filters["source_type"] = source_type
    if cve_id.strip():
        filters["cve_id"] = cve_id.strip()
    return filters or None


def parse_top_k(raw: str) -> int | None:
    """Parse the top-k box: an integer in ``TOP_K_MIN..TOP_K_MAX``, else None.

    Out-of-range input is rejected rather than clamped, so the user sees that
    their value was not used.  Only ASCII digits count: ``int()`` would also
    accept things like Arabic-Indic digits, which the API's own parsing may
    not agree with.
    """
    cleaned = raw.strip()
    if not (cleaned.isascii() and cleaned.isdigit()):
        return None
    value = int(cleaned)
    return value if TOP_K_MIN <= value <= TOP_K_MAX else None
