"""Tests for the TUI's pure formatting helpers.

No Textual and no event loop: everything here is a plain function, which is
why the logic lives in this module rather than in the widgets.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from grimoire.api.schemas import (
    CitationResponse,
    QueryRequest,
    QueryResponse,
    SearchResponse,
    SearchResultItem,
)
from grimoire.tui.formatting import (
    DOC_STATUSES,
    FILE_TYPES,
    SEVERITIES,
    SOURCE_TYPES,
    TOP_K_MAX,
    TOP_K_MIN,
    SourceView,
    build_filter_dict,
    display_url,
    format_size,
    format_timestamp,
    parse_top_k,
    sources_from_query,
    sources_from_search,
    truncate,
)


class TestFormatSize:
    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            (0, "0 B"),
            (1, "1 B"),
            (1023, "1023 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024**2, "1.0 MB"),
            (5 * 1024**2 + 512 * 1024, "5.5 MB"),
            (1024**3, "1.0 GB"),
        ],
    )
    def test_known_values(self, size: int, expected: str) -> None:
        assert format_size(size) == expected

    def test_rounding_up_to_the_next_unit_never_shows_1024(self) -> None:
        # 1048575 B is 1023.999 KB, which rounds to "1024.0 KB" without the bump.
        assert format_size(1024**2 - 1) == "1.0 MB"

    def test_caps_at_gigabytes(self) -> None:
        assert format_size(10**12) == "931.3 GB"
        assert format_size(5 * 1024**4).endswith(" GB")

    def test_negative_sizes_render_as_a_dash(self) -> None:
        assert format_size(-1) == "-"


class TestFormatTimestamp:
    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_missing_values_render_as_a_dash(self, missing: str | None) -> None:
        assert format_timestamp(missing) == "-"

    def test_formats_naive_isoformat(self) -> None:
        assert format_timestamp("2026-10-01T14:03:59.123456") == "2026-10-01 14:03"

    def test_formats_a_date_with_no_time(self) -> None:
        assert format_timestamp("2026-10-01") == "2026-10-01 00:00"

    def test_keeps_the_wall_clock_of_timezone_aware_values(self) -> None:
        """No silent conversion: the table shows what the server sent."""
        assert format_timestamp("2026-10-01T10:00:00+02:00") == "2026-10-01 10:00"
        assert format_timestamp("2026-10-01T10:00:00Z") == "2026-10-01 10:00"

    @pytest.mark.parametrize("garbage", ["yesterday", "2026-13-45", "[red]x[/red]"])
    def test_garbage_renders_as_a_dash_and_is_never_echoed(self, garbage: str) -> None:
        assert format_timestamp(garbage) == "-"


class TestTruncate:
    def test_short_text_is_unchanged(self) -> None:
        assert truncate("hello", 10) == "hello"

    def test_exact_width_is_unchanged(self) -> None:
        assert truncate("hello", 5) == "hello"

    def test_long_text_gets_a_single_ellipsis_within_the_width(self) -> None:
        result = truncate("hello world", 8)
        assert result == "hello w…"
        assert len(result) == 8

    def test_width_one_is_just_the_ellipsis(self) -> None:
        assert truncate("hello", 1) == "…"

    @pytest.mark.parametrize("width", [0, -1, -100])
    def test_non_positive_width_is_empty_never_a_negative_slice(
        self, width: int
    ) -> None:
        assert truncate("hello", width) == ""

    def test_collapses_whitespace_so_a_cell_is_always_one_line(self) -> None:
        assert truncate("a\n\n  b\tc", 20) == "a b c"

    def test_empty_string(self) -> None:
        assert truncate("", 5) == ""


class TestSourcesFromQuery:
    def _response(self, *citations: CitationResponse) -> QueryResponse:
        return QueryResponse(query="q", answer="a", citations=list(citations))

    def test_maps_every_field(self) -> None:
        resp = self._response(
            CitationResponse(
                document_id="doc-1",
                document_title="Sigma primer",
                chunk_id="chunk-9",
                chunk_index=3,
                content_snippet="Sigma is a rule format.",
                relevance_score=0.91,
            )
        )

        assert sources_from_query(resp) == [
            SourceView(
                title="Sigma primer",
                text="Sigma is a rule format.",
                score=0.91,
                document_id="doc-1",
                chunk_ref="chunk 3",
            )
        ]

    def test_missing_title_becomes_untitled(self) -> None:
        resp = self._response(
            CitationResponse(document_id="d", document_title=None, chunk_id="c")
        )

        assert sources_from_query(resp)[0].title == "(untitled)"

    def test_blank_title_becomes_untitled(self) -> None:
        resp = self._response(
            CitationResponse(document_id="d", document_title="  ", chunk_id="c")
        )

        assert sources_from_query(resp)[0].title == "(untitled)"

    def test_falls_back_to_the_chunk_id_without_an_index(self) -> None:
        resp = self._response(CitationResponse(document_id="d", chunk_id="chunk-9"))

        assert sources_from_query(resp)[0].chunk_ref == "chunk-9"

    def test_chunk_index_zero_is_a_real_index_not_missing(self) -> None:
        resp = self._response(
            CitationResponse(document_id="d", chunk_id="chunk-9", chunk_index=0)
        )

        assert sources_from_query(resp)[0].chunk_ref == "chunk 0"

    def test_text_is_never_truncated(self) -> None:
        long_text = "x" * 5000
        resp = self._response(
            CitationResponse(document_id="d", chunk_id="c", content_snippet=long_text)
        )

        assert sources_from_query(resp)[0].text == long_text

    def test_empty_citations_give_an_empty_list(self) -> None:
        assert sources_from_query(self._response()) == []

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_scores_become_none(self, bad: float) -> None:
        resp = self._response(
            CitationResponse(document_id="d", chunk_id="c", relevance_score=bad)
        )

        assert sources_from_query(resp)[0].score is None

    def test_preserves_order(self) -> None:
        resp = self._response(
            CitationResponse(document_id="1", chunk_id="a"),
            CitationResponse(document_id="2", chunk_id="b"),
        )

        assert [s.document_id for s in sources_from_query(resp)] == ["1", "2"]


class TestSourcesFromSearch:
    def test_maps_every_field(self) -> None:
        resp = SearchResponse(
            query="q",
            results=[
                SearchResultItem(
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    document_title="Primer",
                    content="full chunk text",
                    score=0.8,
                )
            ],
        )

        assert sources_from_search(resp) == [
            SourceView(
                title="Primer",
                text="full chunk text",
                score=0.8,
                document_id="doc-1",
                chunk_ref="chunk-1",
            )
        ]

    def test_missing_title_ids_and_empty_results(self) -> None:
        resp = SearchResponse(query="q", results=[SearchResultItem()])

        (source,) = sources_from_search(resp)
        assert source.title == "(untitled)"
        assert source.document_id == ""
        assert source.chunk_ref == ""

        assert sources_from_search(SearchResponse(query="q")) == []

    def test_non_finite_score_becomes_none(self) -> None:
        resp = SearchResponse(query="q", results=[SearchResultItem(score=math.nan)])

        assert sources_from_search(resp)[0].score is None


class TestBuildFilterDict:
    def _call(self, **overrides: Any) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {
            "tags": "",
            "source_type": None,
            "severity": None,
            "cve_id": "",
        }
        kwargs.update(overrides)
        return build_filter_dict(**kwargs)

    def test_nothing_set_returns_none_so_the_client_omits_the_key(self) -> None:
        assert self._call() is None

    def test_whitespace_only_values_count_as_unset(self) -> None:
        assert self._call(tags="  , ,", cve_id="   ") is None

    @pytest.mark.parametrize("blank", [None, ""])
    def test_blank_selects_count_as_unset(self, blank: str | None) -> None:
        assert self._call(source_type=blank, severity=blank) is None

    def test_every_filter_uses_the_servers_key_names(self) -> None:
        assert self._call(
            tags="a,b", source_type="playbook", severity="high", cve_id="CVE-2024-1"
        ) == {
            "tags": ["a", "b"],
            "source_type": "playbook",
            "severity": "high",
            "cve_id": "CVE-2024-1",
        }

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a, ,b", ["a", "b"]),
            ("  spaced  ", ["spaced"]),
            ("one", ["one"]),
            (",,a,,", ["a"]),
            ("a,a", ["a", "a"]),  # no dedupe: identical to the CLI's behaviour
        ],
    )
    def test_tag_splitting(self, raw: str, expected: list[str]) -> None:
        assert self._call(tags=raw) == {"tags": expected}

    def test_matches_the_cli_helper_for_the_same_inputs(self) -> None:
        """Drift guard: the TUI re-implements this to avoid the CLI's heavy imports."""
        cli_query = pytest.importorskip("grimoire.cli.query")

        cases = [
            ("a, b", "playbook", "high", "CVE-2024-1"),
            ("", None, None, ""),
            ("only", None, "low", ""),
            ("", "nvd_cve", None, "CVE-2023-9"),
        ]
        for tags, source_type, severity, cve_id in cases:
            expected = cli_query._build_filter_dict(
                tuple(t.strip() for t in tags.split(",") if t.strip()),
                severity=severity,
                tactic=None,
                technique=None,
                source_type=source_type,
                cve_id=cve_id or None,
                content_date_after=None,
                platforms=(),
            )
            assert (
                self._call(
                    tags=tags,
                    source_type=source_type,
                    severity=severity,
                    cve_id=cve_id,
                )
                == expected
            )


class TestParseTopK:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1", 1), ("5", 5), (" 7 ", 7), ("100", 100), ("007", 7)],
    )
    def test_valid(self, raw: str, expected: int) -> None:
        assert parse_top_k(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["0", "101", "-1", "+5", "5.0", "x", "", "  ", "1e2", "٣", "5 5"],
    )
    def test_invalid_is_none_never_clamped(self, raw: str) -> None:
        assert parse_top_k(raw) is None

    def test_bounds_match_the_servers_request_schema(self) -> None:
        """Drift guard: the API rejects anything outside these with a 422."""
        meta = {
            type(m).__name__: m for m in QueryRequest.model_fields["top_k"].metadata
        }
        assert meta["Ge"].ge == TOP_K_MIN
        assert meta["Le"].le == TOP_K_MAX


class TestChoiceConstants:
    def test_are_non_empty_unique_tuples(self) -> None:
        for values in (SOURCE_TYPES, SEVERITIES, DOC_STATUSES, FILE_TYPES):
            assert isinstance(values, tuple)
            assert values
            assert len(set(values)) == len(values)

    def test_doc_statuses_and_file_types_match_the_database_enums(self) -> None:
        models = pytest.importorskip("grimoire.db.models")

        assert set(DOC_STATUSES) == {s.value for s in models.ProcessingStatus}
        assert set(FILE_TYPES) == {t.value for t in models.FileType}

    def test_source_types_and_severities_match_the_security_enums(self) -> None:
        corpus = pytest.importorskip("grimoire.strategies.security.corpus")
        metadata = pytest.importorskip("grimoire.strategies.security.metadata")

        assert set(SOURCE_TYPES) == {t.value for t in corpus.SourceType}
        assert set(SEVERITIES) == {s.value for s in metadata.Severity}


class TestDisplayUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http://localhost:8001", "http://localhost:8001"),
            ("https://box.example:9000/api", "https://box.example:9000"),
            ("http://user:s3cret@host:1", "http://host:1"),
            ("http://user@host", "http://host"),
            ("http://a@b@host:1", "http://host:1"),
        ],
    )
    def test_strips_credentials_and_paths(self, raw: str, expected: str) -> None:
        assert display_url(raw) == expected

    def test_never_contains_the_password(self) -> None:
        assert "s3cret" not in display_url("http://user:s3cret@host:1")

    @pytest.mark.parametrize("bad", ["http://[red]:1", "http://[::1", "http://[x]/"])
    def test_unparseable_urls_do_not_raise(self, bad: str) -> None:
        assert display_url(bad) == "(invalid URL)"

    def test_ipv6_hosts_keep_their_brackets(self) -> None:
        assert display_url("http://[::1]:8001/x") == "http://[::1]:8001"
