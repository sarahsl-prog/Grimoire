"""Phase 9 — API filter plumbing tests.

These exercise the schema-level helpers and route-level merge logic
directly, without going through ``TestClient`` (which is environment-
sensitive in this sandbox because of the rate-limit / Redis stack).
"""

from __future__ import annotations

import logging

from grimoire.api.routes.query import _merge_security_filters
from grimoire.api.schemas import SECURITY_FILTER_KEYS, warn_unknown_filter_keys


class TestSecurityFilterKeys:
    def test_constant_lists_documented_keys(self) -> None:
        """The plan calls out these specific keys — guard the contract."""
        assert SECURITY_FILTER_KEYS == frozenset(
            {
                "severity",
                "mitre_tactic",
                "mitre_technique_id",
                "source_type",
                "cve_id",
                "content_date_after",
                "platforms",
            }
        )

    def test_constant_is_frozen(self) -> None:
        """Tests pinning the type — accidental mutation must fail."""
        assert isinstance(SECURITY_FILTER_KEYS, frozenset)


class TestWarnUnknownFilterKeys:
    def test_none_filter_is_silent(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            warn_unknown_filter_keys(None)
        assert caplog.records == []

    def test_empty_filter_is_silent(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            warn_unknown_filter_keys({})
        assert caplog.records == []

    def test_all_known_keys_silent(self, caplog) -> None:
        # ``tags`` is an existing pre-Phase-9 key and is explicitly allowed.
        with caplog.at_level(logging.WARNING):
            warn_unknown_filter_keys({"severity": "high", "tags": ["a"]})
        assert caplog.records == []

    def test_unknown_key_warns(self) -> None:
        # The warning is emitted via Loguru — capture it by hooking the sink.
        from loguru import logger as loguru_logger

        messages: list[str] = []
        sink_id = loguru_logger.add(
            lambda msg: messages.append(str(msg)), level="WARNING"
        )
        try:
            warn_unknown_filter_keys({"weird_filter_key": "x"})
        finally:
            loguru_logger.remove(sink_id)
        assert any("weird_filter_key" in m for m in messages)

    def test_unknown_keys_are_not_rejected(self) -> None:
        """Function must return cleanly (warning only, no raise)."""
        warn_unknown_filter_keys({"made_up": 1, "severity": "high"})


class TestMergeSecurityFilters:
    def test_all_none_returns_none(self) -> None:
        out = _merge_security_filters(
            None,
            severity=None,
            mitre_tactic=None,
            mitre_technique_id=None,
            source_type=None,
            cve_id=None,
            content_date_after=None,
            platforms=None,
        )
        assert out is None

    def test_query_param_only(self) -> None:
        out = _merge_security_filters(
            None,
            severity="high",
            mitre_tactic=None,
            mitre_technique_id=None,
            source_type=None,
            cve_id=None,
            content_date_after=None,
            platforms=None,
        )
        assert out == {"severity": "high"}

    def test_body_filter_dict_only(self) -> None:
        out = _merge_security_filters(
            {"tags": ["x"]},
            severity=None,
            mitre_tactic=None,
            mitre_technique_id=None,
            source_type=None,
            cve_id=None,
            content_date_after=None,
            platforms=None,
        )
        assert out == {"tags": ["x"]}

    def test_body_wins_on_conflict(self) -> None:
        """Body ``filter_dict`` overrides query-string values."""
        out = _merge_security_filters(
            {"severity": "critical"},
            severity="low",
            mitre_tactic=None,
            mitre_technique_id=None,
            source_type=None,
            cve_id=None,
            content_date_after=None,
            platforms=None,
        )
        assert out == {"severity": "critical"}

    def test_platforms_csv_split(self) -> None:
        out = _merge_security_filters(
            None,
            severity=None,
            mitre_tactic=None,
            mitre_technique_id=None,
            source_type=None,
            cve_id=None,
            content_date_after=None,
            platforms="windows, linux,macos",
        )
        assert out == {"platforms": ["windows", "linux", "macos"]}

    def test_platforms_csv_drops_blanks(self) -> None:
        out = _merge_security_filters(
            None,
            severity=None,
            mitre_tactic=None,
            mitre_technique_id=None,
            source_type=None,
            cve_id=None,
            content_date_after=None,
            platforms="windows,,",
        )
        assert out == {"platforms": ["windows"]}

    def test_all_query_params_compose(self) -> None:
        out = _merge_security_filters(
            None,
            severity="high",
            mitre_tactic="execution",
            mitre_technique_id="T1059",
            source_type="sigma_rule",
            cve_id="CVE-2024-1",
            content_date_after="2024-01-01",
            platforms="windows",
        )
        assert out == {
            "severity": "high",
            "mitre_tactic": "execution",
            "mitre_technique_id": "T1059",
            "source_type": "sigma_rule",
            "cve_id": "CVE-2024-1",
            "content_date_after": "2024-01-01",
            "platforms": ["windows"],
        }
