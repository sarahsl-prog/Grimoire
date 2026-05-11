"""MITRE ATT&CK parser (Phase 5).

Supports two input flavours:

1. **STIX 2.1 JSON** — ``attack-pattern`` objects from the MITRE/cti repo.
2. **Markdown with YAML frontmatter** — exports with ``kind: attack-pattern`` or
   ``attack_id: T<digits>``.

Each parsed technique yields a list of ``(section_text, SecurityMetadata)``
tuples so the chunker can split by section (Description, Detection, Mitigations,
etc.) while sharing the same technique-level metadata.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from grimoire.strategies.security.corpus import SourceType
from grimoire.strategies.security.metadata import SecurityMetadata, Severity

__all__ = ["parse_mitre"]

_RE_MITRE_TECHNIQUE_ID = re.compile(r"^T\d{4}(?:\.\d{3})?$")


def _extract_mitre_id(
    text: str, stix_obj: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    r"""Try to find a ``T\d{4}(\.\d{3})?`` id from STIX external_references or text."""

    if stix_obj is not None:
        refs = stix_obj.get("external_references", [])
        for ref in refs:
            if isinstance(ref, dict):
                ext_id = ref.get("external_id", "")
                if _RE_MITRE_TECHNIQUE_ID.match(ext_id):
                    return ext_id
                # URL fallback — MITRE urls use /techniques/TXXXX or /techniques/TXXXX/NNN
                url = ref.get("url", "")
                m = re.search(r"/techniques/([T\d\.]+)", url)
                if m:
                    tid = m.group(1)
                    if _RE_MITRE_TECHNIQUE_ID.match(tid):
                        return tid

    # Fallback: scan raw text.
    for m in _RE_MITRE_TECHNIQUE_ID.finditer(text):
        return m.group(0)
    return None


def _extract_tactic(stix_obj: Dict[str, Any]) -> Optional[str]:
    """Return the first tactic from ``kill_chain_phases`` or ``x_mitre_tactic_type``."""

    # kill_chain_phases is the canonical STIX location.
    phases = stix_obj.get("kill_chain_phases", [])
    if isinstance(phases, list):
        for phase in phases:
            if isinstance(phase, dict):
                tactic = phase.get("phase_name")
                if isinstance(tactic, str) and tactic:
                    return tactic.lower().replace(" ", "_")

    # Some ATT&CK objects have a denormalised field.
    tactic = stix_obj.get("x_mitre_tactic_type")
    if isinstance(tactic, str) and tactic:
        return tactic.lower().replace(" ", "_")
    return None


def _extract_platforms(stix_obj: Dict[str, Any]) -> List[str]:
    """Return platforms from ``x_mitre_platforms`` as lower-case strings."""

    raw = stix_obj.get("x_mitre_platforms", [])
    if isinstance(raw, list):
        return [
            str(p).lower()
            for p in raw
            if isinstance(p, (str,)) or isinstance(p, (int, float))
        ]
    return []


def _extract_detection(stix_obj: Dict[str, Any]) -> Optional[str]:
    """Return detection guidance if present."""

    det = stix_obj.get("x_mitre_detection")
    if isinstance(det, str) and det.strip():
        return det.strip()
    return None


def _extract_name(stix_obj: Dict[str, Any]) -> Optional[str]:
    """Return the ATT&CK technique name."""

    name = stix_obj.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _extract_description(stix_obj: Dict[str, Any]) -> Optional[str]:
    """Return the technique description."""

    desc = stix_obj.get("description")
    if isinstance(desc, str) and desc.strip():
        return desc.strip()
    return None


def _extract_mitigations(stix_obj: Dict[str, Any]) -> List[str]:
    """Return mitigation guidance if present in ``x_mitre_mitigations``."""

    raw = stix_obj.get("x_mitre_mitigations")
    if isinstance(raw, list):
        return [str(m) for m in raw if isinstance(m, str) and m.strip()]
    return []


def _parse_stix_attack_pattern(
    obj: Dict[str, Any],
) -> List[Tuple[str, SecurityMetadata]]:
    """Parse a single STIX ``attack-pattern`` object into section tuples."""

    mitre_id = _extract_mitre_id("", stix_obj=obj)
    tactic = _extract_tactic(obj)
    platforms = _extract_platforms(obj)
    name = _extract_name(obj) or "Unknown Technique"
    description = _extract_description(obj)
    detection = _extract_detection(obj)
    mitigations = _extract_mitigations(obj)

    base_meta = SecurityMetadata(
        source_type=SourceType.MITRE_ATTACK,
        mitre_technique_id=mitre_id,
        mitre_tactic=tactic,
        platforms=platforms,
        severity=Severity.UNKNOWN,  # ATT&CK doesn't assign severity natively.
    )

    results: List[Tuple[str, SecurityMetadata]] = []

    if description:
        text = (
            f"Technique: {name}\nID: {mitre_id or 'N/A'}\nDescription:\n{description}"
        )
        results.append((text, base_meta))

    if detection:
        det_text = (
            f"Technique: {name}\nID: {mitre_id or 'N/A'}\nDetection:\n{detection}"
        )
        results.append((det_text, base_meta))

    if mitigations:
        mit_text = (
            f"Technique: {name}\nID: {mitre_id or 'N/A'}\nMitigations:\n"
            + "\n".join(f"- {m}" for m in mitigations)
        )
        results.append((mit_text, base_meta))

    return results


def _parse_stix_bundle(text: str) -> Optional[List[Tuple[str, SecurityMetadata]]]:
    """Attempt to parse ``text`` as a STIX 2.1 bundle."""

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict) or data.get("type") != "bundle":
        return None

    objects = data.get("objects", [])
    if not isinstance(objects, list):
        return None

    results: List[Tuple[str, SecurityMetadata]] = []
    for obj in objects:
        if isinstance(obj, dict) and obj.get("type") == "attack-pattern":
            results.extend(_parse_stix_attack_pattern(obj))

    return results if results else None


def _parse_mitre_markdown(text: str) -> Optional[List[Tuple[str, SecurityMetadata]]]:
    """Attempt to parse ``text`` as a Markdown file with YAML frontmatter."""

    if not text.startswith("---\n"):
        return None

    end = text.find("\n---", 4)
    if end == -1:
        return None

    frontmatter = text[4:end]
    body = text[end + 4 :].lstrip()

    # Extract attack_id from frontmatter.
    attack_id_match = re.search(
        r"^attack_id:\s*(T\d{4}(?:\.\d{3})?)$", frontmatter, re.MULTILINE
    )
    attack_id = attack_id_match.group(1) if attack_id_match else None

    # Fallback: scan body.
    if not attack_id:
        for m in _RE_MITRE_TECHNIQUE_ID.finditer(body):
            attack_id = m.group(0)
            break

    # tactic from frontmatter.
    tactic_match = re.search(r"^tactic:\s*(.+)$", frontmatter, re.MULTILINE)
    tactic = (
        tactic_match.group(1).strip().lower().replace(" ", "_")
        if tactic_match
        else None
    )

    # platforms from frontmatter.
    plat_match = re.search(r"^platforms:\s*(.+)$", frontmatter, re.MULTILINE)
    platforms = (
        [p.strip().lower() for p in plat_match.group(1).split(",")]
        if plat_match
        else []
    )

    name_match = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
    name = name_match.group(1).strip() if name_match else "Unknown Technique"

    base_meta = SecurityMetadata(
        source_type=SourceType.MITRE_ATTACK,
        mitre_technique_id=attack_id,
        mitre_tactic=tactic,
        platforms=platforms,
        severity=Severity.UNKNOWN,
    )

    # Split body by H2 sections (## ...) to create per-section chunks.
    # If no H2 headers, treat the whole body as a single description chunk.
    # Use a regex that tolerates blank lines after the heading.
    section_pattern = re.compile(r"\n##\s+(.+?)(?:\n|$)")
    splits = list(section_pattern.finditer(body))

    if not splits:
        # No H2 sections — whole body is the description.
        if body.strip():
            text = f"Technique: {name}\nID: {attack_id or 'N/A'}\nDescription:\n{body.strip()}"
            return [(text, base_meta)]
        return None

    results: List[Tuple[str, SecurityMetadata]] = []
    # Include the leading text before the first H2 as a "Description" section.
    first_start = splits[0].start()
    if first_start > 0:
        lead = body[:first_start].strip()
        if lead:
            lead_text = (
                f"Technique: {name}\nID: {attack_id or 'N/A'}\nDescription:\n{lead}"
            )
            results.append((lead_text, base_meta))

    for i, match in enumerate(splits):
        heading = match.group(1).strip()
        start = match.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(body)
        section_body = body[start:end].strip()
        if not section_body:
            continue

        section_text = (
            f"Technique: {name}\n"
            f"ID: {attack_id or 'N/A'}\n"
            f"Section: {heading}\n"
            f"{section_body}"
        )
        results.append((section_text, base_meta))

    return results if results else None


def parse_mitre(text: str) -> List[Tuple[str, SecurityMetadata]]:
    """Parse MITRE ATT&CK content in either STIX or Markdown form.

    Args:
        text: Raw document text (STIX JSON bundle or Markdown with frontmatter).

    Returns:
        List of ``(section_text, SecurityMetadata)`` tuples. Each tuple
        represents one logical section of the technique (Description,
        Detection, Mitigations, etc.). Empty text / whitespace returns ``[]``.
        Non-MITRE content that cannot be parsed as STIX or Markdown raises
        ``ValueError``.
    """

    if not text or not text.strip():
        return []

    # Try STIX first (JSON is unambiguous).
    stix_result = _parse_stix_bundle(text)
    if stix_result is not None:
        return stix_result

    # Fall back to Markdown.
    md_result = _parse_mitre_markdown(text)
    if md_result is not None:
        return md_result

    raise ValueError(
        "Text does not appear to be MITRE ATT&CK STIX or Markdown content."
    )
