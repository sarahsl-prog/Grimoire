"""Playbook parser for the security-domain ingestion pipeline.

Parses incident-response "playbooks" — markdown documents with optional YAML
front matter and canonical section structure (``## Trigger``, ``## Actions``,
``## Containment``/``## Recovery`` etc.). Each major section becomes one chunk
so that semantic search can target individual phases of a response procedure.

Document shape (both halves optional independently for parsing, although
front matter + sections together give the best metadata):

.. code-block:: markdown

    ---
    title: Ransomware Containment
    playbook: ransomware
    phase: contain
    severity: critical
    action_type: manual
    trigger: Ransomware encryption activity detected on a host
    mitre_technique_id: T1486
    platforms: [windows, linux]
    ---

    # Ransomware Containment

    ## Trigger
    ...

    ## Actions
    ...

Only level-2 (``##``) headers split sections; deeper headers stay inside their
parent section. The top-of-file preamble (between the ``# Title`` and the
first ``##``) is attached to the first section to avoid orphan text.

Deterministic — no I/O, no LLM, no network.  Never raises; malformed input
yields an empty list.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

import yaml
from loguru import logger

from grimoire.strategies.security.corpus import SourceType
from grimoire.strategies.security.metadata import SecurityMetadata, Severity

__all__ = ["parse_playbook", "playbook_severity_from_string"]


# Split on level-2 headers, capturing the header text as group 1.
_RE_L2_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_RE_MITRE_ID = re.compile(r"^T\d{4}(?:\.\d{3})?$")
_RE_CVE_ID = re.compile(r"^CVE-\d{4}-\d+$")

# Valid IR phases; anything else passes through as None.
_PLAYBOOK_PHASES = {
    "identify",
    "detect",
    "contain",
    "eradicate",
    "recover",
    "prepare",
    "lessons-learned",
}


def playbook_severity_from_string(raw: Optional[str]) -> Severity:
    """Map a front-matter severity string to the shared :class:`Severity` bucket."""
    if not raw or not isinstance(raw, str):
        return Severity.UNKNOWN
    lookup = {
        "critical": Severity.CRITICAL,
        "high": Severity.HIGH,
        "medium": Severity.MEDIUM,
        "low": Severity.LOW,
        "info": Severity.INFO,
        "informational": Severity.INFO,
    }
    return lookup.get(raw.strip().lower(), Severity.UNKNOWN)


def _split_front_matter(text: str) -> Tuple[dict[str, Any], str]:
    """Return ``(front_matter_dict, body)`` for ``---``-fenced documents.

    Malformed YAML logs a warning and is treated as no front matter.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end]
    body = text[end + 4 :].lstrip("\r\n")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.warning("Playbook YAML front matter parse failed: {}", exc)
        return {}, text
    if not isinstance(parsed, dict):
        return {}, body
    return parsed, body


def _split_sections(body: str) -> List[Tuple[str, str]]:
    """Split markdown body into ``(header, content)`` for each ``##`` section.

    Preamble text before the first ``##`` is ignored (it's the title block).
    """
    matches = list(_RE_L2_SECTION.finditer(body))
    sections: List[Tuple[str, str]] = []
    for i, match in enumerate(matches):
        header = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        if content:
            sections.append((header, content))
    return sections


def _str_list(value: Any) -> List[str]:
    """Coerce a front-matter field into a list of strings."""
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _extract_metadata(front_matter: dict[str, Any]) -> SecurityMetadata:
    """Build :class:`SecurityMetadata` from parsed front matter."""
    phase = front_matter.get("phase")
    if isinstance(phase, str):
        phase = phase.strip().lower()
        if phase not in _PLAYBOOK_PHASES:
            logger.debug("Unknown playbook phase {!r}; keeping JSONB value", phase)

    mitre_id = front_matter.get("mitre_technique_id") or front_matter.get("attack_id")
    if isinstance(mitre_id, str) and not _RE_MITRE_ID.match(mitre_id.strip()):
        mitre_id = None

    cve_id = front_matter.get("cve_id")
    if isinstance(cve_id, str) and not _RE_CVE_ID.match(cve_id.strip()):
        cve_id = None

    trigger = front_matter.get("trigger")
    action_type = front_matter.get("action_type")

    return SecurityMetadata(
        source_type=SourceType.PLAYBOOK,
        severity=playbook_severity_from_string(front_matter.get("severity")),
        playbook_phase=phase if isinstance(phase, str) else None,
        action_type=str(action_type).strip().lower() if action_type else None,
        trigger=str(trigger).strip() if trigger else None,
        mitre_technique_id=mitre_id.strip() if isinstance(mitre_id, str) else None,
        cve_id=cve_id.strip() if isinstance(cve_id, str) else None,
        platforms=_str_list(front_matter.get("platforms")),
        source_url=(
            front_matter.get("source_url")
            if isinstance(front_matter.get("source_url"), str)
            else None
        ),
    )


def _section_text(header: str, content: str) -> str:
    """Render one section into search-friendly text."""
    return f"{header}\n\n{content}"


def parse_playbook(text: str) -> List[Tuple[str, SecurityMetadata]]:
    """Parse a playbook document into ``(section_text, SecurityMetadata)`` tuples.

    A document must contain at least one ``##`` section to parse; otherwise the
    result is empty (the corpus detector will already have classified it).

    Args:
        text: Raw document text (markdown with optional YAML front matter).

    Returns:
        List of ``(section_text, SecurityMetadata)``.  Each section shares the
        same metadata (front matter is document-level), plus the section's
        trigger text overrides ``trigger`` when the front matter omitted it.
    """
    if not text or not text.strip():
        return []

    front_matter, body = _split_front_matter(text)
    sections = _split_sections(body)
    if not sections:
        return []

    base_meta = _extract_metadata(front_matter)

    results: List[Tuple[str, SecurityMetadata]] = []
    for header, content in sections:
        meta = base_meta.model_copy()
        # A "Trigger" section with no front-matter trigger adopts its content
        # summary so the facet is still searchable.
        if meta.trigger is None and header.lower() == "trigger":
            first_line = content.splitlines()[0].strip() if content else ""
            meta = meta.model_copy(update={"trigger": first_line or None})
        results.append((_section_text(header, content), meta))

    return results
