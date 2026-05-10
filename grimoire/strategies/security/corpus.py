"""Source-type detection for security-domain ingestion.

This module exposes a single deterministic helper, :func:`detect_source_type`,
that classifies a piece of text into one of the :class:`SourceType` values.
The function is **pure**: no I/O, no network calls, no LLM invocations, no
configuration lookup. Downstream code (the security chunker dispatch in
Phase 3+) can rely on it as a stable contract.

Detection rules are evaluated in strict priority order; the first rule that
matches wins:

1. **Path hints** — case-insensitive substrings inside ``source_metadata["path"]``
   or ``source_metadata["source_path"]`` (e.g. ``/sigma-rules/``,
   ``/nvd-cve/``, ``/mitre-attack/``, ``/iocs/``). ``/mitre-defend/`` is
   explicitly excluded from the MITRE ATT&CK match.
2. **Extension hints** — ``.yml`` / ``.yaml`` files whose body contains both
   top-level ``detection:`` and ``logsource:`` keys are tagged as Sigma rules.
3. **JSON shape sniff** — text that strips to ``{`` or ``[`` is parsed; the
   resulting object is checked for NVD CVE shapes (modern wrapper, legacy
   ``CVE-...`` key, bulk feed) and STIX bundles containing
   ``attack-pattern`` objects.
4. **Markdown frontmatter** — ``---``-fenced frontmatter containing
   ``kind: attack-pattern`` or ``attack_id: T<digits>`` is tagged MITRE ATT&CK.
5. **Filename hint** — basenames matching ``T1059``, ``T1059.001``,
   ``T1059.md``, ``T1059.001.md`` are tagged MITRE ATT&CK.
6. **IOC content sniff** — bodies under 64 KB whose non-empty lines are
   ≥80% IOC-like (IPv4, domain, MD5/SHA1/SHA256) are tagged ``IOC_LIST``.
7. **Fallback** — :data:`SourceType.PROSE` for prose-shaped text,
   :data:`SourceType.UNKNOWN` for empty or trivially short input.

The function is O(n) in text length and capped at 64 KB for the IOC
sniff so even multi-MB CVE feeds resolve in well under 50 ms.
"""

from __future__ import annotations

import json
import os
import re
from enum import Enum
from typing import Any, Mapping, Optional

# ---------------------------------------------------------------------------
# Public enum
# ---------------------------------------------------------------------------


class SourceType(str, Enum):
    """Coarse-grained category for an ingested document.

    Values are stable strings safe to persist in DB / vector-store metadata.
    """

    NVD_CVE = "nvd_cve"
    SIGMA_RULE = "sigma_rule"
    MITRE_ATTACK = "mitre_attack"
    IOC_LIST = "ioc_list"
    PROSE = "prose"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Module-level compiled regexes (declared once for performance)
# ---------------------------------------------------------------------------

# Sigma extension-hint regexes (multiline so `^` matches each YAML line start).
_RE_YAML_DETECTION = re.compile(r"^\s*detection:", re.MULTILINE)
_RE_YAML_LOGSOURCE = re.compile(r"^\s*logsource:", re.MULTILINE)

# CVE id matcher used in JSON shape and frontmatter rules.
_RE_CVE_ID = re.compile(r"^CVE-\d{4}-\d+$")

# MITRE technique id (with optional sub-technique) used in frontmatter scan.
_RE_ATTACK_ID = re.compile(r"^T\d{4}(?:\.\d{3})?$")

# Filename basenames of the form `T1059`, `T1059.md`, `T1059.001`,
# `T1059.001.md`. We allow a trailing extension (anything non-empty).
_RE_FILENAME_TECHNIQUE = re.compile(r"^T\d{4}(?:\.\d{3})?(?:\..+)?$")

# Frontmatter detection helpers.
_RE_FRONTMATTER_KIND = re.compile(r"^\s*kind:\s*attack-pattern\s*$", re.MULTILINE)
_RE_FRONTMATTER_ATTACK_ID = re.compile(
    r"^\s*attack_id:\s*T\d{4}(?:\.\d{3})?\s*$", re.MULTILINE
)

# IOC sniff regexes — anchored with fullmatch via `$` so a stray prose word
# does not accidentally match.
_RE_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# Domain: at least two labels, alnum + hyphens, TLD letters only, 2+ chars.
_RE_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)
_RE_MD5 = re.compile(r"^[a-fA-F0-9]{32}$")
_RE_SHA1 = re.compile(r"^[a-fA-F0-9]{40}$")
_RE_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")

# Path-hint substrings (case-insensitive) per source type. Order within the
# checker matters — see ``_check_path_hints``.
_PATH_HINTS_SIGMA = ("/sigma-rules/", "/sigma/")
_PATH_HINTS_NVD = ("/nvd-cve/", "/nvd/", "/cve/")
_PATH_HINTS_MITRE = ("/mitre-attack/", "/attack/", "/mitre/")
_PATH_HINTS_MITRE_EXCLUDE = ("/mitre-defend/",)
_PATH_HINTS_IOC = ("/iocs/", "/ioc-lists/")

# Extensions that warrant the YAML sniff.
_YAML_EXTENSIONS = (".yml", ".yaml")

# Cap on bytes scanned for the IOC heuristic.
_IOC_SNIFF_BYTE_CAP = 64 * 1024

# IOC sniff thresholds.
_IOC_MIN_NONEMPTY_LINES = 2
_IOC_MATCH_RATIO = 0.80


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_path(source_metadata: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Return the original-case path string from metadata, or ``None``."""

    if not source_metadata:
        return None
    for key in ("path", "source_path"):
        value = (
            source_metadata.get(key) if isinstance(source_metadata, Mapping) else None
        )
        if isinstance(value, str) and value:
            return value
    return None


def _check_path_hints(path: Optional[str]) -> Optional[SourceType]:
    """Apply the path-substring matching rules. Returns ``None`` on no match."""

    if not path:
        return None
    haystack = path.lower()

    if any(hint in haystack for hint in _PATH_HINTS_SIGMA):
        return SourceType.SIGMA_RULE
    if any(hint in haystack for hint in _PATH_HINTS_NVD):
        return SourceType.NVD_CVE
    # MITRE check explicitly excludes the D3FEND-adjacent path.
    if not any(excl in haystack for excl in _PATH_HINTS_MITRE_EXCLUDE) and any(
        hint in haystack for hint in _PATH_HINTS_MITRE
    ):
        return SourceType.MITRE_ATTACK
    if any(hint in haystack for hint in _PATH_HINTS_IOC):
        return SourceType.IOC_LIST
    return None


def _check_extension_hints(path: Optional[str], text: str) -> Optional[SourceType]:
    """Combined extension + content sniff for Sigma YAML rules."""

    if not path:
        return None
    if not path.lower().endswith(_YAML_EXTENSIONS):
        return None
    if _RE_YAML_DETECTION.search(text) and _RE_YAML_LOGSOURCE.search(text):
        return SourceType.SIGMA_RULE
    return None


def _check_json_shape(text: str) -> Optional[SourceType]:
    """Parse ``text`` as JSON and infer source type from object shape.

    Returns ``None`` if the text does not look like JSON or doesn't match a
    known shape. Never raises.
    """

    stripped = text.lstrip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None

    if not isinstance(obj, dict):
        return None

    # Modern NVD wrapper: {"cve": {"id": "CVE-YYYY-NNNN", ...}}
    cve_block = obj.get("cve")
    if isinstance(cve_block, dict):
        cve_id = cve_block.get("id")
        if isinstance(cve_id, str) and _RE_CVE_ID.match(cve_id):
            return SourceType.NVD_CVE

    # Legacy NVD shape: {"CVE-YYYY-NNNN": {...}}
    for key in obj.keys():
        if isinstance(key, str) and _RE_CVE_ID.match(key):
            return SourceType.NVD_CVE

    # Bulk NVD feed: {"vulnerabilities": [{"cve": {"id": "CVE-..."}}, ...]}
    vulns = obj.get("vulnerabilities")
    if isinstance(vulns, list):
        for item in vulns:
            if not isinstance(item, dict):
                continue
            cve_block = item.get("cve")
            if isinstance(cve_block, dict):
                cve_id = cve_block.get("id")
                if isinstance(cve_id, str) and _RE_CVE_ID.match(cve_id):
                    return SourceType.NVD_CVE

    # STIX bundle with attack-pattern objects.
    if obj.get("type") == "bundle":
        objects = obj.get("objects")
        if isinstance(objects, list):
            for item in objects:
                if isinstance(item, dict) and item.get("type") == "attack-pattern":
                    return SourceType.MITRE_ATTACK

    return None


def _check_frontmatter(text: str) -> Optional[SourceType]:
    """Detect MITRE ATT&CK markdown via YAML frontmatter."""

    if not text.startswith("---\n"):
        return None
    # Find closing fence; second occurrence of "\n---" (allowing trailing newline).
    end = text.find("\n---", 4)
    if end == -1:
        return None
    frontmatter = text[4:end]
    if _RE_FRONTMATTER_KIND.search(frontmatter):
        return SourceType.MITRE_ATTACK
    if _RE_FRONTMATTER_ATTACK_ID.search(frontmatter):
        return SourceType.MITRE_ATTACK
    return None


def _check_filename_hint(path: Optional[str]) -> Optional[SourceType]:
    """Detect MITRE ATT&CK technique from basename like ``T1059.md``."""

    if not path:
        return None
    base = os.path.basename(path)
    if _RE_FILENAME_TECHNIQUE.match(base):
        return SourceType.MITRE_ATTACK
    return None


def _is_ioc_line(line: str) -> bool:
    """Return True if ``line`` (already stripped) looks like a single IOC."""

    if not line:
        return False
    # Allow common bracketed obfuscation: 1.2.3[.]4 / example[.]com
    candidate = line.replace("[.]", ".").replace("(.)", ".")
    if _RE_IPV4.match(candidate):
        # Validate octet ranges.
        try:
            return all(0 <= int(part) <= 255 for part in candidate.split("."))
        except ValueError:
            return False
    if _RE_MD5.match(candidate):
        return True
    if _RE_SHA1.match(candidate):
        return True
    if _RE_SHA256.match(candidate):
        return True
    if _RE_DOMAIN.match(candidate):
        return True
    return False


def _sniff_iocs(text: str) -> bool:
    """Return True if ``text`` looks like an IOC list."""

    if len(text) > _IOC_SNIFF_BYTE_CAP:
        return False
    nonempty = [line.strip() for line in text.splitlines() if line.strip()]
    if len(nonempty) < _IOC_MIN_NONEMPTY_LINES:
        return False
    matches = sum(1 for line in nonempty if _is_ioc_line(line))
    ratio = matches / len(nonempty)
    return ratio >= _IOC_MATCH_RATIO


def _looks_like_prose(text: str) -> bool:
    """Heuristic: any non-trivial line longer than 60 chars suggests prose."""

    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) > 60:
            return True
    # Single long unbroken line (no newlines) also counts as prose.
    if "\n" not in text and len(text.strip()) > 60:
        return True
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def detect_source_type(
    text: str,
    source_metadata: Optional[Mapping[str, Any]] = None,
) -> SourceType:
    """Best-effort detection of what kind of security content ``text`` represents.

    Args:
        text: Raw document text. May be empty.
        source_metadata: Optional mapping with file context. Recognized keys
            are ``path`` and ``source_path`` (string). Extra keys are ignored.

    Returns:
        A :class:`SourceType`. Never raises; on any internal failure the
        function falls through to the ``PROSE`` / ``UNKNOWN`` branch.
    """

    # Defensive empty check first — saves a dozen regex calls on empties.
    if text is None or not text.strip():
        return SourceType.UNKNOWN

    path = _extract_path(source_metadata)

    # 1. Path hints
    hit = _check_path_hints(path)
    if hit is not None:
        return hit

    # 2. Extension hint (Sigma)
    hit = _check_extension_hints(path, text)
    if hit is not None:
        return hit

    # 3. JSON shape
    hit = _check_json_shape(text)
    if hit is not None:
        return hit

    # 4. Markdown frontmatter
    hit = _check_frontmatter(text)
    if hit is not None:
        return hit

    # 5. Filename hint (T1059.md etc.)
    hit = _check_filename_hint(path)
    if hit is not None:
        return hit

    # 6. IOC sniff
    if _sniff_iocs(text):
        return SourceType.IOC_LIST

    # 7. Prose vs unknown fallback
    if _looks_like_prose(text):
        return SourceType.PROSE
    return SourceType.UNKNOWN


__all__ = ["SourceType", "detect_source_type"]
