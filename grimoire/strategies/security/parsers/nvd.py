"""NVD CVE parser for the security-domain ingestion pipeline.

Phase 4 introduces deterministic parsing of NVD JSON 2.0 records. The module
supports three input shapes:

1. **Modern single-record wrapper** ``{"cve": {...}}`` (NVD API 2.0).
2. **Bulk feed** ``{"vulnerabilities": [{"cve": {...}}, ...]}`` (annual
   download / API paged response).
3. **Legacy key-value** ``{"CVE-YYYY-NNNN": {...}}`` (older bulk dumps).

Each record is extracted into a ``(text, SecurityMetadata)`` tuple. The text
is a human-readable summary; the metadata captures the structured facets
needed for filtering and re-ranking.

Design notes:

* CVSS is extracted in priority order: v3.1 → v3.0 → v2.0. The first one
  found wins.
* Severity is taken from ``baseSeverity`` when available; if absent it is
  mapped from the numeric ``baseScore``.
* CPE product names are extracted from the ``cpe23Uri``/``criteria`` field.
* The parser is **pure**: no I/O after the text/dict is passed in.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from loguru import logger

from grimoire.strategies.security.corpus import SourceType
from grimoire.strategies.security.metadata import SecurityMetadata, Severity

__all__ = ["parse_cve", "parse_nvd_json", "severity_from_cvss_score"]


# ---------------------------------------------------------------------------
# CVSS helpers
# ---------------------------------------------------------------------------


def severity_from_cvss_score(score: Optional[float]) -> Severity:
    """Map a CVSS base score to :class:`Severity`.

    Mapping follows the standard CVSS v3 buckets:

    * 0.0 → :attr:`Severity.INFO`
    * 0.1–3.9 → :attr:`Severity.LOW`
    * 4.0–6.9 → :attr:`Severity.MEDIUM`
    * 7.0–8.9 → :attr:`Severity.HIGH`
    * 9.0–10.0 → :attr:`Severity.CRITICAL`

    Args:
        score: CVSS base score in [0, 10].

    Returns:
        Mapped severity, or :attr:`Severity.UNKNOWN` when ``score`` is
        missing or out of range.
    """

    if score is None:
        return Severity.UNKNOWN
    if score < 0.0 or score > 10.0:
        return Severity.UNKNOWN
    if score == 0.0:
        return Severity.INFO
    if score <= 3.9:
        return Severity.LOW
    if score <= 6.9:
        return Severity.MEDIUM
    if score <= 8.9:
        return Severity.HIGH
    return Severity.CRITICAL


def _extract_cvss(record: dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """Return ``(base_score, base_severity)`` from the CVE record.

    Tries v3.1 → v3.0 → v2.0 in that order.
    """

    metrics = record.get("metrics")
    if not isinstance(metrics, dict):
        return None, None

    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        metric_list = metrics.get(key)
        if not isinstance(metric_list, list) or not metric_list:
            continue
        # Prefer the "Primary" source; otherwise take the first entry.
        entry: Optional[dict[str, Any]] = None
        for m in metric_list:
            if isinstance(m, dict) and m.get("type") == "Primary":
                entry = m
                break
        if entry is None:
            entry = metric_list[0]
        if not isinstance(entry, dict):
            continue
        cvss_data = entry.get("cvssData")
        if not isinstance(cvss_data, dict):
            continue
        score = cvss_data.get("baseScore")
        severity = cvss_data.get("baseSeverity")
        if isinstance(score, (int, float)):
            return float(score), severity if isinstance(severity, str) else None

    return None, None


# ---------------------------------------------------------------------------
# CPE / product helpers
# ---------------------------------------------------------------------------


def _extract_cpe_product(cpe_uri: str) -> Optional[str]:
    """Extract a human-readable product string from a CPE 2.3 URI.

    ``cpe:2.3:a:vendor:product:version:...`` → ``"vendor product"``.
    Falls back to the raw URI if parsing fails.
    """

    # Split on ':'; a well-formed CPE 2.3 has at least 7 parts.
    parts = cpe_uri.split(":")
    if len(parts) >= 6:
        vendor = parts[3] if parts[3] != "*" else None
        product = parts[4] if parts[4] != "*" else None
        if vendor and product:
            return f"{vendor} {product}"
        if product:
            return product
    # Fallback: strip the cpe prefix.
    return cpe_uri


def _extract_affected_products(record: dict[str, Any]) -> List[str]:
    """Collect human-readable product names from CPE matches."""

    products: List[str] = []
    seen: set[str] = set()

    configurations = record.get("configurations")
    if not isinstance(configurations, list):
        return products

    for cfg in configurations:
        if not isinstance(cfg, dict):
            continue
        nodes = cfg.get("nodes")
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if not isinstance(node, dict):
                continue
            cpe_matches = node.get("cpeMatch")
            if not isinstance(cpe_matches, list):
                continue
            for match in cpe_matches:
                if not isinstance(match, dict):
                    continue
                criteria = match.get("criteria")
                if isinstance(criteria, str) and criteria:
                    product = _extract_cpe_product(criteria)
                    if product and product not in seen:
                        seen.add(product)
                        products.append(product)

    return products


# ---------------------------------------------------------------------------
# CWE helpers
# ---------------------------------------------------------------------------


def _extract_cwe_ids(record: dict[str, Any]) -> List[str]:
    """Scrape CWE identifiers from the weaknesses block."""

    cwe_ids: List[str] = []
    seen: set[str] = set()

    weaknesses = record.get("weaknesses")
    if not isinstance(weaknesses, list):
        return cwe_ids

    for weakness in weaknesses:
        if not isinstance(weakness, dict):
            continue
        descriptions = weakness.get("description")
        if not isinstance(descriptions, list):
            continue
        for desc in descriptions:
            if not isinstance(desc, dict):
                continue
            value = desc.get("value")
            if isinstance(value, str) and value:
                # Accept "CWE-79" or plain "79".
                m = re.search(r"CWE-(\d+)", value, re.IGNORECASE)
                if m:
                    cwe_id = f"CWE-{m.group(1)}"
                    if cwe_id not in seen:
                        seen.add(cwe_id)
                        cwe_ids.append(cwe_id)

    return cwe_ids


# ---------------------------------------------------------------------------
# Description / references helpers
# ---------------------------------------------------------------------------


def _extract_description(record: dict[str, Any]) -> str:
    """Return the English description, or the first available one."""

    descriptions = record.get("descriptions")
    if not isinstance(descriptions, list):
        return ""

    for desc in descriptions:
        if isinstance(desc, dict) and desc.get("lang") == "en":
            value = desc.get("value")
            if isinstance(value, str):
                return value

    # Fallback: first description regardless of language.
    for desc in descriptions:
        if isinstance(desc, dict):
            value = desc.get("value")
            if isinstance(value, str):
                return value

    return ""


def _extract_references(record: dict[str, Any]) -> List[str]:
    """Collect reference URLs."""

    refs: List[str] = []
    references = record.get("references")
    if not isinstance(references, list):
        return refs
    for ref in references:
        if isinstance(ref, dict):
            url = ref.get("url")
            if isinstance(url, str) and url:
                refs.append(url)
    return refs


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _parse_iso_date(raw: Any) -> Optional[datetime]:
    """Parse an ISO 8601 datetime string, normalising to UTC."""

    if not isinstance(raw, str) or not raw:
        return None
    try:
        # NVD uses "2024-01-15T00:00:00.000" (no timezone).
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_cve(record: dict[str, Any]) -> Tuple[str, SecurityMetadata]:
    """Parse a single NVD CVE record into a human-readable summary + metadata.

    Args:
        record: A dict representing the ``cve`` block from an NVD record.

    Returns:
        ``(text, SecurityMetadata)`` where ``text`` is a formatted summary
        and ``SecurityMetadata`` holds the structured fields.
    """

    cve_id = record.get("id", "")
    if not isinstance(cve_id, str):
        cve_id = ""

    description = _extract_description(record)
    score, severity_str = _extract_cvss(record)
    severity: Severity
    if isinstance(severity_str, str) and severity_str:
        severity = Severity(severity_str.lower())
    else:
        severity = severity_from_cvss_score(score)

    cwe_ids = _extract_cwe_ids(record)
    products = _extract_affected_products(record)
    refs = _extract_references(record)
    published = _parse_iso_date(record.get("published"))

    # Build human-readable text.
    parts: List[str] = []
    if cve_id:
        parts.append(f"CVE: {cve_id}")
    if description:
        parts.append(f"Description: {description}")
    if score is not None:
        parts.append(f"CVSS Score: {score}")
    if severity_str:
        parts.append(f"Severity: {severity_str}")
    if cwe_ids:
        parts.append(f"CWEs: {', '.join(cwe_ids)}")
    if products:
        parts.append(f"Affected Products: {', '.join(products)}")
    if refs:
        parts.append(f"References: {', '.join(refs)}")
    if published:
        parts.append(f"Published: {published.isoformat()}")

    text = "\n".join(parts)

    meta = SecurityMetadata(
        source_type=SourceType.NVD_CVE,
        cve_id=cve_id,
        cvss_score=score,
        severity=severity,
        cwe_ids=cwe_ids,
        affected_products=products,
        published_date=published,
        content_date=published,
    )

    return text, meta


def parse_nvd_json(
    text_or_obj: str | dict[str, Any],
) -> List[Tuple[str, SecurityMetadata]]:
    """Parse NVD JSON 2.0 input (single record, bulk feed, or legacy shape).

    Args:
        text_or_obj: Raw JSON string or already-parsed dict.

    Returns:
        List of ``(text, SecurityMetadata)`` — one per CVE. On any parse
        failure the function logs a warning and returns an empty list; it
        never raises.
    """

    obj: dict[str, Any]
    if isinstance(text_or_obj, str):
        import json

        if not text_or_obj.strip():
            return []
        try:
            parsed = json.loads(text_or_obj)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("NVD JSON parse failed: {}", exc)
            return []
        if not isinstance(parsed, dict):
            logger.warning("NVD JSON top-level is not a dict")
            return []
        obj = parsed
    elif isinstance(text_or_obj, dict):
        obj = text_or_obj
    else:
        logger.warning("NVD parse received unexpected type: {}", type(text_or_obj))
        return []

    results: List[Tuple[str, SecurityMetadata]] = []

    # Shape 1: Bulk feed with vulnerabilities list.
    vulnerabilities = obj.get("vulnerabilities")
    if isinstance(vulnerabilities, list):
        for item in vulnerabilities:
            if not isinstance(item, dict):
                continue
            cve_block = item.get("cve")
            if not isinstance(cve_block, dict):
                continue
            try:
                results.append(parse_cve(cve_block))
            except Exception as exc:
                cve_id = cve_block.get("id", "unknown")
                logger.warning("Failed to parse CVE {}: {}", cve_id, exc)
        return results

    # Shape 2: Modern single-record wrapper.
    cve_block = obj.get("cve")
    if isinstance(cve_block, dict):
        try:
            results.append(parse_cve(cve_block))
        except Exception as exc:
            cve_id = cve_block.get("id", "unknown")
            logger.warning("Failed to parse CVE {}: {}", cve_id, exc)
        return results

    # Shape 3: Legacy key-value: {"CVE-YYYY-NNNN": {...}}
    for key, value in obj.items():
        if not isinstance(value, dict):
            continue
        # Fabricate a minimal cve-shaped dict.
        synthetic: dict[str, Any] = {
            "id": key if isinstance(key, str) else None,
            **value,
        }
        try:
            results.append(parse_cve(synthetic))
        except Exception as exc:
            logger.warning("Failed to parse legacy CVE {}: {}", key, exc)

    return results
