"""Tests for the Phase 2 :class:`SecurityMetadata` schema.

The schema is pure data — no I/O, no DB. These tests cover:

* defaults (the always-on filterable defaults: source_type, severity,
  tlp_level),
* JSON round-tripping via :meth:`pydantic.BaseModel.model_dump_json`,
* validators (CVE id shape, CVSS bounds, MITRE technique id shape),
* :meth:`SecurityMetadata.to_chromadb_metadata` shape rules
  (scalars-only, pipe-joined lists, list cap, ISO datetimes),
* :meth:`SecurityMetadata.to_db_columns` keys and types,
* :meth:`SecurityMetadata.from_db_row` round-trip via a stub object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from grimoire.strategies.security.corpus import SourceType
from grimoire.strategies.security.metadata import (
    SecurityMetadata,
    Severity,
    TLPLevel,
)


# ---------------------------------------------------------------------------
# 1. Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    """The always-present trio (source_type, severity, tlp_level) and empties."""

    def test_construct_with_all_defaults(self) -> None:
        meta = SecurityMetadata()
        assert meta.source_type is SourceType.UNKNOWN
        assert meta.severity is Severity.UNKNOWN
        assert meta.tlp_level is TLPLevel.WHITE
        # Optional scalar fields default to None.
        assert meta.cve_id is None
        assert meta.cvss_score is None
        assert meta.source_url is None
        assert meta.published_date is None
        assert meta.content_date is None
        assert meta.mitre_technique_id is None
        assert meta.mitre_tactic is None
        assert meta.mitre_subtechnique is None
        # List fields default to empty lists (independent instances).
        assert meta.cwe_ids == []
        assert meta.affected_products == []
        assert meta.threat_actors == []
        assert meta.malware_families == []
        assert meta.ioc_types == []
        assert meta.detection_categories == []
        assert meta.platforms == []
        assert meta.log_sources == []

    def test_default_lists_are_independent(self) -> None:
        """Mutating one default list must not affect the next instance."""

        a = SecurityMetadata()
        a.cwe_ids.append("CWE-1")
        b = SecurityMetadata()
        assert b.cwe_ids == []


# ---------------------------------------------------------------------------
# 2. JSON round-trip
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    """Round-tripping through model_dump_json keeps every field equal."""

    def test_round_trip_preserves_fields(self) -> None:
        original = SecurityMetadata(
            source_type=SourceType.NVD_CVE,
            cve_id="CVE-2024-12345",
            cvss_score=9.8,
            severity=Severity.CRITICAL,
            cwe_ids=["CWE-79", "CWE-89"],
            affected_products=["Acme Widget 1.0"],
            published_date=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            mitre_technique_id="T1059.001",
            mitre_tactic="execution",
            threat_actors=["APT-Foo"],
            platforms=["windows", "linux"],
            content_date=datetime(2024, 6, 7, 8, 9, 10, tzinfo=timezone.utc),
        )
        as_json = original.model_dump_json()
        restored = SecurityMetadata.model_validate_json(as_json)
        assert restored == original


# ---------------------------------------------------------------------------
# 3. Validators
# ---------------------------------------------------------------------------


class TestValidators:
    """Validators reject malformed values."""

    def test_bad_cve_id_raises(self) -> None:
        with pytest.raises(ValueError):
            SecurityMetadata(cve_id="CVE-bad")

    def test_good_cve_id_accepted(self) -> None:
        meta = SecurityMetadata(cve_id="CVE-2024-1")
        assert meta.cve_id == "CVE-2024-1"

    @pytest.mark.parametrize("score", [-0.1, 10.1, 100.0])
    def test_cvss_score_out_of_range_raises(self, score: float) -> None:
        with pytest.raises(ValueError):
            SecurityMetadata(cvss_score=score)

    @pytest.mark.parametrize("score", [0.0, 5.5, 10.0])
    def test_cvss_score_in_range_accepted(self, score: float) -> None:
        meta = SecurityMetadata(cvss_score=score)
        assert meta.cvss_score == score

    @pytest.mark.parametrize(
        "bad_id",
        ["T123", "T12345", "T1059.0001", "1059", "TT1059"],
    )
    def test_bad_mitre_technique_id_raises(self, bad_id: str) -> None:
        with pytest.raises(ValueError):
            SecurityMetadata(mitre_technique_id=bad_id)

    @pytest.mark.parametrize("good_id", ["T1059", "T1059.001", "T9999.999"])
    def test_good_mitre_technique_id_accepted(self, good_id: str) -> None:
        meta = SecurityMetadata(mitre_technique_id=good_id)
        assert meta.mitre_technique_id == good_id

    def test_naive_datetime_promoted_to_utc(self) -> None:
        naive = datetime(2024, 1, 2, 3, 4, 5)
        meta = SecurityMetadata(content_date=naive)
        assert meta.content_date is not None
        assert meta.content_date.tzinfo is timezone.utc

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValueError):
            SecurityMetadata(unknown_field="boom")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 4. to_chromadb_metadata
# ---------------------------------------------------------------------------


class TestToChromaDBMetadata:
    """Flat dict shape required by the vector store."""

    def test_only_scalars_and_strings(self) -> None:
        meta = SecurityMetadata(
            cwe_ids=["CWE-1", "CWE-2"],
            threat_actors=["APT-A"],
            platforms=["windows"],
            content_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        out = meta.to_chromadb_metadata()
        for key, value in out.items():
            assert isinstance(value, (str, int, float, bool)), (
                f"{key!r} must be a ChromaDB scalar, got {type(value)}"
            )
            assert not isinstance(value, list), f"{key!r} must not be a list"

    def test_lists_are_pipe_joined(self) -> None:
        meta = SecurityMetadata(
            cwe_ids=["CWE-79", "CWE-89"],
            threat_actors=["APT-A", "APT-B"],
            platforms=["windows", "linux", "macos"],
        )
        out = meta.to_chromadb_metadata()
        assert out["cwe_ids"] == "CWE-79|CWE-89"
        assert out["threat_actors"] == "APT-A|APT-B"
        assert out["platforms"] == "windows|linux|macos"

    def test_lists_truncated_at_32_entries(self) -> None:
        big_list = [f"CWE-{i}" for i in range(50)]
        meta = SecurityMetadata(cwe_ids=big_list)
        out = meta.to_chromadb_metadata()
        joined = out["cwe_ids"]
        assert isinstance(joined, str)
        assert len(joined.split("|")) == 32
        assert joined.split("|")[0] == "CWE-0"
        assert joined.split("|")[-1] == "CWE-31"

    def test_defaults_render_correctly(self) -> None:
        out = SecurityMetadata().to_chromadb_metadata()
        # Always-present filterable trio.
        assert out["source_type"] == "unknown"
        assert out["severity"] == "unknown"
        assert out["tlp_level"] == "white"
        # Empty scalars / lists.
        assert out["cve_id"] == ""
        assert out["cvss_score"] == 0.0
        assert out["mitre_technique_id"] == ""
        assert out["content_date"] == ""
        assert out["source_url"] == ""
        assert out["cwe_ids"] == ""
        assert out["threat_actors"] == ""
        assert out["platforms"] == ""

    def test_datetime_serialised_iso8601(self) -> None:
        when = datetime(2024, 6, 7, 8, 9, 10, tzinfo=timezone.utc)
        meta = SecurityMetadata(content_date=when)
        out = meta.to_chromadb_metadata()
        # Pydantic / stdlib isoformat: '2024-06-07T08:09:10+00:00'
        assert out["content_date"] == when.isoformat()

    def test_required_keys_always_present(self) -> None:
        out = SecurityMetadata().to_chromadb_metadata()
        for key in (
            "source_type",
            "severity",
            "tlp_level",
            "cve_id",
            "cvss_score",
            "cwe_ids",
            "mitre_technique_id",
            "mitre_tactic",
            "threat_actors",
            "platforms",
            "content_date",
            "source_url",
        ):
            assert key in out, f"{key!r} missing from ChromaDB metadata"


# ---------------------------------------------------------------------------
# 5. to_db_columns + from_db_row
# ---------------------------------------------------------------------------


@dataclass
class _StubDoc:
    """Minimal stand-in for a SQLAlchemy ``Document`` row."""

    source_type: Optional[str] = None
    cve_id: Optional[str] = None
    severity: Optional[Severity] = None
    mitre_technique_id: Optional[str] = None
    content_date: Optional[datetime] = None
    tlp_level: Optional[TLPLevel] = None
    security_metadata: Optional[dict[str, Any]] = field(default=None)


class TestDbColumnsRoundTrip:
    """Indexed-column dict + stub-row hydration."""

    def test_to_db_columns_keys(self) -> None:
        meta = SecurityMetadata(
            source_type=SourceType.NVD_CVE,
            cve_id="CVE-2024-1",
            severity=Severity.CRITICAL,
            mitre_technique_id="T1059.001",
            content_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            tlp_level=TLPLevel.AMBER,
        )
        cols = meta.to_db_columns()
        assert set(cols.keys()) == {
            "source_type",
            "cve_id",
            "severity",
            "mitre_technique_id",
            "content_date",
            "tlp_level",
        }
        assert cols["source_type"] == "nvd_cve"
        assert cols["cve_id"] == "CVE-2024-1"
        assert cols["severity"] is Severity.CRITICAL
        assert cols["mitre_technique_id"] == "T1059.001"
        assert cols["tlp_level"] is TLPLevel.AMBER
        assert cols["content_date"] == datetime(2024, 1, 1, tzinfo=timezone.utc)

    def test_from_db_row_round_trip(self) -> None:
        original = SecurityMetadata(
            source_type=SourceType.MITRE_ATTACK,
            mitre_technique_id="T1059",
            severity=Severity.HIGH,
            tlp_level=TLPLevel.GREEN,
            content_date=datetime(2024, 5, 1, tzinfo=timezone.utc),
            cwe_ids=["CWE-1"],
            threat_actors=["APT-Round"],
        )
        # Simulate persistence: scalar columns + JSON blob.
        stub = _StubDoc(
            source_type=original.source_type.value,
            cve_id=original.cve_id,
            severity=original.severity,
            mitre_technique_id=original.mitre_technique_id,
            content_date=original.content_date,
            tlp_level=original.tlp_level,
            security_metadata=original.model_dump(mode="json"),
        )
        restored = SecurityMetadata.from_db_row(stub)
        assert restored == original

    def test_from_db_row_with_only_indexed_columns(self) -> None:
        """If JSONB blob is None, indexed scalars should still hydrate."""

        stub = _StubDoc(
            source_type=SourceType.NVD_CVE.value,
            cve_id="CVE-2024-2",
            severity=Severity.MEDIUM,
            mitre_technique_id=None,
            content_date=None,
            tlp_level=TLPLevel.WHITE,
            security_metadata=None,
        )
        meta = SecurityMetadata.from_db_row(stub)
        assert meta.source_type is SourceType.NVD_CVE
        assert meta.cve_id == "CVE-2024-2"
        assert meta.severity is Severity.MEDIUM
        assert meta.tlp_level is TLPLevel.WHITE
        # Lists fall back to defaults.
        assert meta.cwe_ids == []
        assert meta.threat_actors == []

    def test_from_db_row_handles_string_enum_values(self) -> None:
        """JSONB stores enums as strings; from_db_row must coerce them back."""

        stub = _StubDoc(
            security_metadata={
                "source_type": "sigma_rule",
                "severity": "high",
                "tlp_level": "amber",
            }
        )
        meta = SecurityMetadata.from_db_row(stub)
        assert meta.source_type is SourceType.SIGMA_RULE
        assert meta.severity is Severity.HIGH
        assert meta.tlp_level is TLPLevel.AMBER
