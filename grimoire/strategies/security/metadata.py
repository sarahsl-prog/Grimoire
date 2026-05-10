"""Security-domain metadata schema (Phase 2).

This module defines :class:`SecurityMetadata` — a Pydantic v2 model that
captures the structured, security-specific facets of an ingested document
(CVE id, CVSS score, MITRE technique, TLP level, threat actors, etc.). It
is **pure data**: no I/O, no DB, no LLM. The metadata feeds three sinks:

* a small set of indexed scalar columns on ``documents`` (for SQL filters
  and joins) — see :meth:`SecurityMetadata.to_db_columns`,
* a wide-but-sparse JSONB blob on ``documents.security_metadata`` for the
  remaining fields (lists, dates, etc.),
* a flat dict of scalars and pipe-joined strings written to ChromaDB
  metadata so vector queries can post-filter — see
  :meth:`SecurityMetadata.to_chromadb_metadata`.

Phase 2 only plumbs the schema and persistence. Population by parsers and
the LLM extractor arrives in Phase 3+.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from grimoire.strategies.security.corpus import SourceType

__all__ = [
    "Severity",
    "TLPLevel",
    "SecurityMetadata",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TLPLevel(str, Enum):
    """Traffic Light Protocol level for sharing restrictions.

    Values follow the FIRST.org TLP 2.0 colour palette but are kept lower
    case to match the rest of Grimoire's enum conventions.
    """

    WHITE = "white"
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class Severity(str, Enum):
    """Coarse severity bucket used across CVSS, Sigma, and ATT&CK content."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_RE_CVE_ID = re.compile(r"^CVE-\d{4}-\d+$")
_RE_MITRE_TECHNIQUE_ID = re.compile(r"^T\d{4}(\.\d{3})?$")

# ChromaDB only stores scalars; we cap pipe-joined list strings to bound
# metadata payload size. 32 entries comfortably covers realistic CWE,
# affected-product, and platform lists without truncating typical content.
_LIST_ENTRY_CAP = 32


def _ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Promote naive datetimes to UTC; pass through TZ-aware values."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _join_list(values: List[str]) -> str:
    """Pipe-join a list with truncation at ``_LIST_ENTRY_CAP`` entries."""

    if not values:
        return ""
    if len(values) > _LIST_ENTRY_CAP:
        logger.debug(
            "SecurityMetadata list truncated from {} to {} entries",
            len(values),
            _LIST_ENTRY_CAP,
        )
        values = values[:_LIST_ENTRY_CAP]
    return "|".join(values)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class SecurityMetadata(BaseModel):
    """Structured security metadata for an ingested document.

    All fields are optional except ``source_type`` (defaults to
    :attr:`SourceType.UNKNOWN`), ``severity`` (defaults to
    :attr:`Severity.UNKNOWN`), and ``tlp_level`` (defaults to
    :attr:`TLPLevel.WHITE`). The trio is always serialized so that
    downstream filters can rely on stable defaults.

    Datetime fields are normalised to UTC: naive inputs are interpreted as
    UTC; TZ-aware inputs pass through unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    # Source classification ---------------------------------------------------
    source_type: SourceType = Field(
        default=SourceType.UNKNOWN,
        description="Coarse-grained source type (sigma_rule, nvd_cve, etc.).",
    )
    source_url: Optional[str] = Field(
        default=None,
        description="Canonical upstream URL for the source document.",
    )
    tlp_level: TLPLevel = Field(
        default=TLPLevel.WHITE,
        description="Traffic Light Protocol level controlling re-sharing.",
    )

    # CVE block ---------------------------------------------------------------
    cve_id: Optional[str] = Field(
        default=None,
        description="CVE identifier, e.g. 'CVE-2024-12345'.",
    )
    cvss_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description="CVSS base score in the [0, 10] range.",
    )
    severity: Severity = Field(
        default=Severity.UNKNOWN,
        description="Severity bucket; mapped from CVSS or Sigma 'level'.",
    )
    cwe_ids: List[str] = Field(
        default_factory=list,
        description="CWE identifiers, e.g. ['CWE-79', 'CWE-89'].",
    )
    affected_products: List[str] = Field(
        default_factory=list,
        description="CPE product strings or human-readable product names.",
    )
    published_date: Optional[datetime] = Field(
        default=None,
        description="Upstream publication date (TZ-aware preferred).",
    )

    # MITRE block -------------------------------------------------------------
    mitre_technique_id: Optional[str] = Field(
        default=None,
        description="ATT&CK technique id, e.g. 'T1059' or 'T1059.001'.",
    )
    mitre_tactic: Optional[str] = Field(
        default=None,
        description="ATT&CK tactic, e.g. 'execution' or 'persistence'.",
    )
    mitre_subtechnique: Optional[str] = Field(
        default=None,
        description="Human-readable sub-technique name (denormalised).",
    )

    # Threat-intel block ------------------------------------------------------
    threat_actors: List[str] = Field(
        default_factory=list,
        description="Named threat actors / APT groups.",
    )
    malware_families: List[str] = Field(
        default_factory=list,
        description="Malware family names referenced by the document.",
    )
    ioc_types: List[str] = Field(
        default_factory=list,
        description="IOC types present (e.g. 'ipv4', 'domain', 'sha256').",
    )

    # Detection block ---------------------------------------------------------
    detection_categories: List[str] = Field(
        default_factory=list,
        description="Detection-rule categories or 'falsepositives' notes.",
    )
    platforms: List[str] = Field(
        default_factory=list,
        description="Platforms covered (e.g. 'windows', 'linux', 'aws').",
    )
    log_sources: List[str] = Field(
        default_factory=list,
        description="Log source channels referenced (Sigma 'logsource').",
    )

    # Recency -----------------------------------------------------------------
    content_date: Optional[datetime] = Field(
        default=None,
        description="Effective date of the content (not ingest date).",
    )

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #

    @field_validator("cve_id")
    @classmethod
    def _validate_cve_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not _RE_CVE_ID.match(value):
            raise ValueError(f"cve_id {value!r} does not match 'CVE-YYYY-N+'")
        return value

    @field_validator("mitre_technique_id")
    @classmethod
    def _validate_mitre_technique_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not _RE_MITRE_TECHNIQUE_ID.match(value):
            raise ValueError(
                f"mitre_technique_id {value!r} does not match 'T<4d>(.<3d>)?'"
            )
        return value

    @field_validator("published_date", "content_date")
    @classmethod
    def _normalise_datetime(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _ensure_utc(value)

    # ------------------------------------------------------------------ #
    # Serialisation helpers
    # ------------------------------------------------------------------ #

    def to_chromadb_metadata(self) -> dict[str, Any]:
        """Flatten to a dict of ChromaDB-friendly scalars and strings.

        ChromaDB metadata cannot store lists or ``None`` — values must be
        ``str``/``int``/``float``/``bool``. This method:

        * pipe-joins list fields (capped at 32 entries),
        * serialises datetimes as ISO 8601 strings,
        * substitutes safe defaults for unset values (``""`` for text,
          ``0.0`` for ``cvss_score``, ``"unknown"`` for ``severity``),
        * always emits ``source_type``, ``severity``, and ``tlp_level`` so
          downstream filters can rely on those keys being present.

        Returns:
            Flat dict whose values are exclusively scalars / strings.
        """

        cve_id = self.cve_id or ""
        source_url = self.source_url or ""
        mitre_technique = self.mitre_technique_id or ""
        mitre_tactic = self.mitre_tactic or ""
        cvss_score = float(self.cvss_score) if self.cvss_score is not None else 0.0
        content_date = (
            self.content_date.isoformat() if self.content_date is not None else ""
        )

        payload: dict[str, Any] = {
            # Always-present filterable defaults.
            "source_type": self.source_type.value,
            "severity": self.severity.value,
            "tlp_level": self.tlp_level.value,
            # Indexed-but-optional scalars (rendered as "" / 0.0 when unset).
            "cve_id": cve_id,
            "cvss_score": cvss_score,
            "mitre_technique_id": mitre_technique,
            "mitre_tactic": mitre_tactic,
            "content_date": content_date,
            "source_url": source_url,
            # Pipe-joined list fields (truncated at the entry cap).
            "cwe_ids": _join_list(self.cwe_ids),
            "threat_actors": _join_list(self.threat_actors),
            "platforms": _join_list(self.platforms),
        }
        return payload

    def to_db_columns(self) -> dict[str, Any]:
        """Return the indexed-scalar column values to set on ``Document``.

        Keys correspond 1:1 to columns added by Alembic migration
        ``0006_add_security_metadata``. Enums are returned as the enum
        instance (SQLAlchemy handles ``values_callable``); datetimes are
        returned TZ-aware; ``None`` flows through.
        """

        return {
            "source_type": self.source_type.value,
            "cve_id": self.cve_id,
            "severity": self.severity,
            "mitre_technique_id": self.mitre_technique_id,
            "content_date": self.content_date,
            "tlp_level": self.tlp_level,
        }

    @classmethod
    def from_db_row(cls, doc: Any) -> "SecurityMetadata":
        """Hydrate a :class:`SecurityMetadata` from a ``Document`` row.

        Reads the indexed scalar columns and merges the wide JSONB blob
        (``doc.security_metadata``) on top. The JSONB blob always wins on
        conflict, since it is the canonical persistence form. Missing
        fields fall back to defaults.

        Args:
            doc: A SQLAlchemy ``Document`` instance (or any object with
                attribute access to the seven fields).

        Returns:
            A populated :class:`SecurityMetadata`.
        """

        # Start from the JSONB blob if present, then layer indexed scalars
        # on top so they remain authoritative for the columns we actually
        # index. The blob may carry list/threat-intel/etc. fields.
        blob: dict[str, Any] = {}
        raw_blob = getattr(doc, "security_metadata", None)
        if isinstance(raw_blob, dict):
            blob.update(raw_blob)

        # Indexed scalars override blob values — they're the source of
        # truth for columns the SQL layer can filter on.
        scalar_overrides = {
            "source_type": getattr(doc, "source_type", None),
            "cve_id": getattr(doc, "cve_id", None),
            "severity": getattr(doc, "severity", None),
            "mitre_technique_id": getattr(doc, "mitre_technique_id", None),
            "content_date": getattr(doc, "content_date", None),
            "tlp_level": getattr(doc, "tlp_level", None),
        }
        for key, value in scalar_overrides.items():
            if value is not None:
                blob[key] = value

        # Ensure enum-typed fields can be initialised from their string
        # values when arriving from JSONB.
        if isinstance(blob.get("source_type"), str):
            try:
                blob["source_type"] = SourceType(blob["source_type"])
            except ValueError:
                blob["source_type"] = SourceType.UNKNOWN
        if isinstance(blob.get("severity"), str):
            try:
                blob["severity"] = Severity(blob["severity"])
            except ValueError:
                blob["severity"] = Severity.UNKNOWN
        if isinstance(blob.get("tlp_level"), str):
            try:
                blob["tlp_level"] = TLPLevel(blob["tlp_level"])
            except ValueError:
                blob["tlp_level"] = TLPLevel.WHITE

        # Drop unknown keys (e.g. legacy fields) so ``extra="forbid"``
        # doesn't reject the construction.
        allowed = set(cls.model_fields.keys())
        cleaned = {k: v for k, v in blob.items() if k in allowed}
        return cls(**cleaned)
