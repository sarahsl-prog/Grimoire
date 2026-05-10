"""Sigma rule parser for the security-domain ingestion pipeline.

Phase 3 introduces deterministic parsing of Sigma YAML rules. Each rule
(delimited by ``---``) is extracted into a ``(text, SecurityMetadata)``
tuple. The text is a human-readable summary of the rule; the metadata
captures the structured facets needed for filtering and re-ranking.

Design notes:

* Uses ``yaml.safe_load_all`` to split multi-doc YAML files.
* ``level`` → :class:`Severity` mapping is hard-coded but small and
  unlikely to change.
* MITRE technique ids are scraped from ``tags`` using the
  ``attack.tNNNN(.NNN)?`` convention.
* Tactics are scraped from ``tags`` using ``attack.<tactic_name>`` where
  the value is not a technique id.
* The parser is **pure**: no I/O after the text is passed in.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

import yaml
from loguru import logger

from grimoire.strategies.security.corpus import SourceType
from grimoire.strategies.security.metadata import SecurityMetadata, Severity

__all__ = ["parse_sigma", "sigma_level_to_severity"]


# Sigma "level" field → Severity mapping.
_LEVEL_MAP = {
    "critical": Severity.CRITICAL,
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
    "informational": Severity.INFO,
    "informational_only": Severity.INFO,
}

# Regex for MITRE technique-id tags: attack.t1059, attack.t1059.001
_RE_TAG_TECHNIQUE = re.compile(r"^attack\.t(\d{4}(?:\.\d{3})?)$", re.IGNORECASE)

# Regex for MITRE tactic tags: attack.execution, attack.persistence, …
# Anything that starts with "attack." but is NOT a technique id.
_RE_TAG_TACTIC = re.compile(r"^attack\.([a-z_]+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sigma_level_to_severity(level: Optional[str]) -> Severity:
    """Map a Sigma ``level`` string to :class:`Severity`.

    Args:
        level: Raw level value from the Sigma rule (e.g. ``"high"``).

    Returns:
        Mapped severity, or :attr:`Severity.UNKNOWN` when ``level`` is
        missing or unrecognised.
    """

    if not level:
        return Severity.UNKNOWN
    return _LEVEL_MAP.get(level.lower().strip(), Severity.UNKNOWN)


def _extract_technique_ids(tags: List[str]) -> List[str]:
    """Scrape MITRE technique ids from Sigma tags.

    Sigma convention is ``attack.t1059`` or ``attack.t1059.001``.
    Returns upper-case ids (``T1059``, ``T1059.001``).
    """

    out: List[str] = []
    for tag in tags:
        m = _RE_TAG_TECHNIQUE.match(tag)
        if m:
            out.append(f"T{m.group(1).upper()}")
    return out


def _extract_tactics(tags: List[str]) -> List[str]:
    """Scrape MITRE tactics from Sigma tags.

    Any ``attack.<word>`` tag that is NOT a technique id is treated as a
    tactic (e.g. ``attack.execution``, ``attack.persistence``).
    """

    out: List[str] = []
    for tag in tags:
        if _RE_TAG_TECHNIQUE.match(tag):
            continue
        m = _RE_TAG_TACTIC.match(tag)
        if m:
            out.append(m.group(1).lower().replace("_", " "))
    return out


def _pluck_logsource(rule: dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Extract ``platforms`` and ``log_sources`` from the ``logsource`` block.

    Returns:
        ``(platforms, log_sources)`` — both as lists of strings.
    """

    logsource = rule.get("logsource")
    if not isinstance(logsource, dict):
        return [], []

    platforms: List[str] = []
    log_sources: List[str] = []

    product = logsource.get("product")
    if isinstance(product, str) and product:
        platforms.append(product.lower())

    category = logsource.get("category")
    if isinstance(category, str) and category:
        # In Sigma, "category" is a broad log-source family (e.g.
        # "process_creation", "network_connection"). We treat it as a
        # log source channel.
        log_sources.append(category.lower())

    service = logsource.get("service")
    if isinstance(service, str) and service:
        log_sources.append(service.lower())

    return platforms, log_sources


def _pluck_detection_categories(rule: dict[str, Any]) -> List[str]:
    """Collect false-positives and condition notes.

    Returns a list of human-readable strings suitable for
    ``SecurityMetadata.detection_categories``.
    """

    out: List[str] = []

    falsepositives = rule.get("falsepositives")
    if isinstance(falsepositives, list):
        for fp in falsepositives:
            if isinstance(fp, str) and fp:
                out.append(fp)
    elif isinstance(falsepositives, str) and falsepositives:
        out.append(falsepositives)

    detection = rule.get("detection")
    if isinstance(detection, dict):
        condition = detection.get("condition")
        if isinstance(condition, str) and condition:
            out.append(f"condition: {condition}")

    return out


def _build_rule_text(rule: dict[str, Any]) -> str:
    """Render a human-readable summary of a Sigma rule.

    The summary is what gets embedded and stored as the chunk content.
    It is intentionally concise but contains all salient fields.
    """

    parts: List[str] = []

    title = rule.get("title")
    if isinstance(title, str) and title:
        parts.append(f"Title: {title}")

    rule_id = rule.get("id")
    if isinstance(rule_id, str) and rule_id:
        parts.append(f"ID: {rule_id}")

    status = rule.get("status")
    if isinstance(status, str) and status:
        parts.append(f"Status: {status}")

    level = rule.get("level")
    if isinstance(level, str) and level:
        parts.append(f"Level: {level}")

    description = rule.get("description")
    if isinstance(description, str) and description:
        parts.append(f"Description: {description}")

    author = rule.get("author")
    if isinstance(author, str) and author:
        parts.append(f"Author: {author}")

    logsource = rule.get("logsource")
    if isinstance(logsource, dict):
        ls_parts: List[str] = []
        for key in ("product", "service", "category"):
            val = logsource.get(key)
            if isinstance(val, str) and val:
                ls_parts.append(f"{key}={val}")
        if ls_parts:
            parts.append(f"Logsource: {' | '.join(ls_parts)}")

    detection = rule.get("detection")
    if isinstance(detection, dict):
        # Render the detection selectors (everything except "condition")
        selectors: List[str] = []
        for key, value in detection.items():
            if key == "condition":
                continue
            selectors.append(f"{key}: {value}")
        if selectors:
            parts.append("Detection:\n  " + "\n  ".join(selectors))
        condition = detection.get("condition")
        if isinstance(condition, str) and condition:
            parts.append(f"Condition: {condition}")

    tags = rule.get("tags")
    if isinstance(tags, list) and tags:
        parts.append(f"Tags: {', '.join(str(t) for t in tags)}")

    falsepositives = rule.get("falsepositives")
    if isinstance(falsepositives, list) and falsepositives:
        parts.append(f"Falsepositives: {', '.join(str(fp) for fp in falsepositives)}")
    elif isinstance(falsepositives, str) and falsepositives:
        parts.append(f"Falsepositives: {falsepositives}")

    references = rule.get("references")
    if isinstance(references, list) and references:
        parts.append(f"References: {', '.join(str(r) for r in references)}")

    return "\n".join(parts)


def parse_sigma(text: str) -> List[Tuple[str, SecurityMetadata]]:
    """Parse a Sigma rule file (possibly multi-doc) into structured metadata.

    Args:
        text: Raw YAML text, possibly containing multiple ``---``-delimited
            Sigma rules.

    Returns:
        List of ``(rule_text, SecurityMetadata)`` tuples — one per rule.
        Rules that cannot be parsed (bad YAML, missing required fields) are
        logged and skipped; the function never raises.
    """

    if not text or not text.strip():
        return []

    results: List[Tuple[str, SecurityMetadata]] = []

    # yaml.safe_load_all handles both single-doc and multi-doc YAML.
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        logger.warning("Sigma YAML parse failed: {}", exc)
        return []

    for idx, rule in enumerate(docs):
        if not isinstance(rule, dict):
            continue

        # Minimum viable Sigma rule: must have *some* title or id.
        title = rule.get("title")
        rule_id = rule.get("id")
        if not (isinstance(title, str) and title) and not (
            isinstance(rule_id, str) and rule_id
        ):
            logger.debug("Skipping doc #{} — no title or id", idx)
            continue

        # Build human-readable text.
        rule_text = _build_rule_text(rule)
        if not rule_text.strip():
            continue

        # Extract structured metadata.
        tags: List[str] = []
        raw_tags = rule.get("tags")
        if isinstance(raw_tags, list):
            tags = [str(t) for t in raw_tags if t]

        platforms, log_sources = _pluck_logsource(rule)
        detection_categories = _pluck_detection_categories(rule)
        technique_ids = _extract_technique_ids(tags)
        tactics = _extract_tactics(tags)

        meta = SecurityMetadata(
            source_type=SourceType.SIGMA_RULE,
            severity=sigma_level_to_severity(rule.get("level")),
            mitre_technique_id=technique_ids[0] if technique_ids else None,
            mitre_tactic=tactics[0] if tactics else None,
            platforms=platforms,
            log_sources=log_sources,
            detection_categories=detection_categories,
        )

        results.append((rule_text, meta))

    return results
