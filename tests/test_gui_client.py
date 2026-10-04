"""Tests for the GUI's HTTP client and its error mapping.

No Qt here either — GrimoireClient is plain httpx, so these run without a
display server.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest

from grimoire.client.client import GrimoireClient
from grimoire.client.config import GuiConfig
from grimoire.client.errors import (
    AuthFailed,
    ConnectionFailed,
    MalformedResponse,
    RateLimited,
    RequestRejected,
    ServerError,
    TimedOut,
)

BASE = "http://testapi:8001"


@pytest.fixture
def config() -> GuiConfig:
    return GuiConfig(base_url=BASE, api_key="grim_agt_test")


@pytest.fixture
def client(config: GuiConfig):
    c = GrimoireClient(config)
    yield c
    c.close()


class TestAsk:
    def test_parses_answer_and_citations(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/query/ask",
            json={
                "query": "what is a sigma rule",
                "answer": "A detection rule format.",
                "citations": [
                    {
                        "document_id": "doc-1",
                        "document_title": "Sigma primer",
                        "chunk_id": "chunk-1",
                        "chunk_index": 0,
                        "content_snippet": "Sigma is...",
                        "relevance_score": 0.91,
                    }
                ],
                "model_used": "llama3",
                "search_results_count": 1,
                "cached": False,
                "duration_ms": 1200,
            },
        )

        result = client.ask("what is a sigma rule", top_k=3)

        assert result.answer == "A detection rule format."
        assert len(result.citations) == 1
        assert result.citations[0].relevance_score == 0.91
        assert result.model_used == "llama3"

    def test_sends_api_key_header_and_body(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/query/ask",
            json={"query": "q", "answer": "a", "citations": []},
        )

        client.ask("q", top_k=7, use_cache=False)

        request = httpx_mock.get_request()
        assert request.headers["X-API-Key"] == "grim_agt_test"
        import json as _json

        body = _json.loads(request.content)
        assert body == {"query": "q", "top_k": 7, "use_cache": False}


class TestSearch:
    def test_parses_results(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/query/search",
            json={
                "query": "sigma",
                "results": [
                    {
                        "chunk_id": "c1",
                        "document_id": "d1",
                        "document_title": "Primer",
                        "content": "Sigma is...",
                        "score": 0.8,
                    }
                ],
                "total_results": 1,
                "duration_ms": 40,
            },
        )

        result = client.search("sigma", top_k=10)

        assert result.total_results == 1
        assert result.results[0].document_title == "Primer"

    def test_default_top_k_matches_the_product_default_of_five(
        self, client, httpx_mock
    ) -> None:
        """search()'s own default must match what the GUI actually sends.

        SearchTab's top_k spinner defaults to 5 (Ruling 17) and always
        passes it explicitly, so this default is never exercised by the
        GUI - but a direct caller of GrimoireClient should get the same
        default the product uses, not a stale value copied from elsewhere.
        """
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/query/search",
            json={
                "query": "sigma",
                "results": [],
                "total_results": 0,
                "duration_ms": 1,
            },
        )

        client.search("sigma")

        import json as _json

        body = _json.loads(httpx_mock.get_request().content)
        assert body["top_k"] == 5


class TestRecentDocuments:
    def test_requests_limit_and_parses_rows(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/documents?offset=0&limit=10",
            json={
                "documents": [
                    {
                        "id": "d1",
                        "title": "Notes",
                        "source_path": "/app/uploads/abc_notes.md",
                        "file_type": "md",
                        "storage_backend": "local",
                        "processing_status": "completed",
                        "size_bytes": 2048,
                        "created_at": "2026-09-21T10:00:00Z",
                        "updated_at": "2026-09-21T10:00:05Z",
                        "tag_count": 3,
                        "chunk_count": 7,
                    }
                ],
                "total": 1,
                "offset": 0,
                "limit": 10,
            },
        )

        result = client.recent_documents(limit=10)

        assert result.total == 1
        assert result.documents[0].chunk_count == 7


class TestUpload:
    def test_posts_multipart_and_parses_result(
        self, client, httpx_mock, tmp_path
    ) -> None:
        sample = tmp_path / "notes.md"
        sample.write_text("# hello")
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/ingest/upload",
            json={
                "file_path": "/app/uploads/abc_notes.md",
                "document_id": "doc-9",
                "status": "completed",
                "chunks_created": 2,
                "vectors_stored": 2,
                "tags_applied": 1,
                "error_message": None,
                "duration_ms": 900,
            },
        )

        result = client.upload(sample, auto_tag=True)

        assert result.document_id == "doc-9"
        assert result.chunks_created == 2
        request = httpx_mock.get_request()
        assert b"notes.md" in request.content
        assert request.headers["content-type"].startswith("multipart/form-data")


class TestErrorMapping:
    def test_connection_refused(self, client, httpx_mock) -> None:
        httpx_mock.add_exception(httpx.ConnectError("refused"))
        with pytest.raises(ConnectionFailed) as exc:
            client.search("x")
        assert BASE in exc.value.message

    def test_timeout(self, client, httpx_mock) -> None:
        httpx_mock.add_exception(httpx.ReadTimeout("slow"))
        with pytest.raises(TimedOut):
            client.ask("x")

    def test_401(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=401, json={"detail": "nope"})
        with pytest.raises(AuthFailed) as exc:
            client.search("x")
        assert "key" in exc.value.message.lower()

    def test_403(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=403, json={"detail": "tier"})
        with pytest.raises(RequestRejected):
            client.search("x")

    def test_413_reports_the_limit(self, client, httpx_mock, tmp_path) -> None:
        sample = tmp_path / "big.pdf"
        sample.write_bytes(b"x" * 16)
        httpx_mock.add_response(
            status_code=413, json={"detail": "File exceeds the maximum upload size"}
        )
        with pytest.raises(RequestRejected) as exc:
            client.upload(sample)
        assert "exceeds" in exc.value.message.lower()

    def test_415(self, client, httpx_mock, tmp_path) -> None:
        sample = tmp_path / "thing.pdf"
        sample.write_bytes(b"x")
        httpx_mock.add_response(
            status_code=415, json={"detail": "Unsupported file type: .pdf"}
        )
        with pytest.raises(RequestRejected) as exc:
            client.upload(sample)
        assert "Unsupported" in exc.value.message

    def test_422_flattens_validation_detail(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            status_code=422,
            json={
                "detail": [
                    {"loc": ["body", "top_k"], "msg": "must be <= 100", "type": "x"}
                ]
            },
        )
        with pytest.raises(RequestRejected) as exc:
            client.search("x")
        assert "top_k" in exc.value.message
        assert "\n" not in exc.value.message

    def test_429_carries_retry_after(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            status_code=429, json={"detail": "slow down"}, headers={"Retry-After": "30"}
        )
        with pytest.raises(RateLimited) as exc:
            client.search("x")
        assert exc.value.retry_after == 30
        assert "30" in exc.value.message

    def test_500(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=500, text="boom")
        with pytest.raises(ServerError):
            client.search("x")

    def test_unparseable_body(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=200, text="not json at all")
        with pytest.raises(MalformedResponse):
            client.search("x")

    def test_schema_mismatch(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=200, json={"unexpected": True})
        with pytest.raises(MalformedResponse):
            client.recent_documents()


class TestUploadPreflight:
    def test_rejects_missing_file_without_a_request(self, client, tmp_path) -> None:
        with pytest.raises(RequestRejected):
            client.upload(tmp_path / "nope.pdf")

    def test_rejects_oversized_file_without_a_request(self, tmp_path) -> None:
        cfg = GuiConfig(base_url=BASE, api_key="k", max_upload_bytes=4)
        c = GrimoireClient(cfg)
        sample = tmp_path / "big.txt"
        sample.write_bytes(b"12345678")
        try:
            with pytest.raises(RequestRejected) as exc:
                c.upload(sample)
            assert "exceeds" in exc.value.message.lower()
        finally:
            c.close()

    def test_rejects_unsupported_extension_without_a_request(
        self, client, tmp_path
    ) -> None:
        sample = tmp_path / "thing.exe"
        sample.write_bytes(b"MZ")
        with pytest.raises(RequestRejected) as exc:
            client.upload(sample)
        assert ".exe" in exc.value.message

    def test_rejects_a_file_that_becomes_unreadable_after_preflight(
        self, client, tmp_path
    ) -> None:
        # TOCTOU: is_file() passes preflight, but the actual open() can still
        # fail (permissions changed, file removed by another process). That
        # must surface as RequestRejected, not a raw OSError.
        if os.geteuid() == 0:
            pytest.skip("root bypasses file permission checks")
        sample = tmp_path / "vanishing.md"
        sample.write_text("# hello")
        sample.chmod(0o000)
        try:
            with pytest.raises(RequestRejected) as exc:
                client.upload(sample)
            assert sample.name in exc.value.message
        finally:
            sample.chmod(0o644)


class TestTimeouts:
    """A bare float widens every phase - including connect - to the long
    read timeout, so the client would hang for minutes reaching a dead
    server instead of failing fast.  These capture the actual timeout object
    handed to httpx, rather than relying on wire-level timing.
    """

    def test_ask_keeps_connect_timeout_short(self, config, monkeypatch) -> None:
        captured: dict[str, Any] = {}
        original_request = httpx.Client.request

        def spy(self: httpx.Client, method: str, url: str, **kwargs: Any):
            captured["timeout"] = kwargs.get("timeout")
            return original_request(self, method, url, **kwargs)

        monkeypatch.setattr(httpx.Client, "request", spy)
        client = GrimoireClient(config)
        try:
            # testapi:8001 does not resolve, so this fails fast regardless
            # of the timeout value - only the captured kwarg is asserted on.
            with pytest.raises(ConnectionFailed):
                client.ask("q")
        finally:
            client.close()

        timeout = captured["timeout"]
        assert timeout.connect == config.connect_timeout
        assert timeout.read == config.long_read_timeout

    def test_upload_keeps_connect_timeout_short(
        self, config, monkeypatch, tmp_path
    ) -> None:
        captured: dict[str, Any] = {}
        original_request = httpx.Client.request

        def spy(self: httpx.Client, method: str, url: str, **kwargs: Any):
            captured["timeout"] = kwargs.get("timeout")
            return original_request(self, method, url, **kwargs)

        monkeypatch.setattr(httpx.Client, "request", spy)
        sample = tmp_path / "notes.md"
        sample.write_text("# hello")
        client = GrimoireClient(config)
        try:
            with pytest.raises(ConnectionFailed):
                client.upload(sample)
        finally:
            client.close()

        timeout = captured["timeout"]
        assert timeout.connect == config.connect_timeout
        assert timeout.read == config.long_read_timeout


class TestClientConstruction:
    def test_malformed_base_url_raises_connection_failed(self) -> None:
        # httpx.InvalidURL does not subclass httpx.HTTPError, so it would
        # otherwise escape __init__ as a raw exception.
        cfg = GuiConfig(base_url="http://[::1", api_key="k")
        with pytest.raises(ConnectionFailed) as exc:
            GrimoireClient(cfg)
        assert "[::1" in exc.value.message


DOC_ROW = {
    "id": "doc-1",
    "title": "Sigma primer",
    "source_path": "/data/sigma.pdf",
    "file_type": "pdf",
    "storage_backend": "local",
    "processing_status": "completed",
    "size_bytes": 2048,
    "created_at": "2026-10-01T10:00:00",
    "updated_at": "2026-10-01T10:05:00",
}


class TestListDocuments:
    def test_sends_only_supplied_params(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            json={"documents": [DOC_ROW], "total": 1, "offset": 50, "limit": 50}
        )

        client.list_documents(offset=50, limit=50, status="failed")

        request = httpx_mock.get_request()
        assert request.url.path == "/api/v1/documents"
        assert dict(request.url.params) == {
            "offset": "50",
            "limit": "50",
            "status": "failed",
        }

    def test_sends_both_filters_when_given(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json={"documents": [], "total": 0})

        client.list_documents(status="completed", file_type="pdf")

        params = dict(httpx_mock.get_request().url.params)
        assert params["status"] == "completed"
        assert params["file_type"] == "pdf"

    def test_sends_the_search_text_as_q(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json={"documents": [], "total": 0})

        client.list_documents(q="kube & co")

        # Encoded by httpx, so a "&" in the text cannot add a query parameter.
        assert dict(httpx_mock.get_request().url.params)["q"] == "kube & co"

    def test_blank_search_text_is_not_sent(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json={"documents": [], "total": 0})

        client.list_documents(q="   ")

        assert "q" not in dict(httpx_mock.get_request().url.params)

    def test_search_text_is_trimmed(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json={"documents": [], "total": 0})

        client.list_documents(q="  postgres  ")

        assert dict(httpx_mock.get_request().url.params)["q"] == "postgres"

    def test_parses_page_metadata(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            json={"documents": [DOC_ROW], "total": 312, "offset": 0, "limit": 50}
        )

        result = client.list_documents()

        assert result.total == 312
        assert result.limit == 50
        assert result.documents[0].title == "Sigma primer"

    def test_recent_documents_still_sends_offset_zero_and_limit(
        self, client, httpx_mock
    ) -> None:
        httpx_mock.add_response(json={"documents": [], "total": 0})

        client.recent_documents(limit=7)

        assert dict(httpx_mock.get_request().url.params) == {
            "offset": "0",
            "limit": "7",
        }


class TestGetDocument:
    def test_parses_detail(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{BASE}/api/v1/documents/doc-1",
            json={**DOC_ROW, "error_message": "parse failed"},
        )

        result = client.get_document("doc-1")

        assert result.id == "doc-1"
        assert result.error_message == "parse failed"

    def test_404_is_a_request_rejected(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            status_code=404, json={"detail": "Document nope not found"}
        )

        with pytest.raises(RequestRejected, match="Not found"):
            client.get_document("nope")

    def test_malformed_body_raises_malformed_response(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json={"unrelated": True})

        with pytest.raises(MalformedResponse):
            client.get_document("doc-1")

    def test_id_is_url_quoted_so_it_cannot_alter_the_path(
        self, client, httpx_mock
    ) -> None:
        httpx_mock.add_response(json=DOC_ROW)

        client.get_document("a/b?x=1")

        raw_path = httpx_mock.get_request().url.raw_path.decode()
        assert raw_path == "/api/v1/documents/a%2Fb%3Fx%3D1"

    @pytest.mark.parametrize("bad_id", ["", "   "])
    def test_blank_id_is_rejected_without_a_request(
        self, client, httpx_mock, bad_id
    ) -> None:
        with pytest.raises(RequestRejected):
            client.get_document(bad_id)

        assert httpx_mock.get_requests() == []


class TestQueryFilters:
    def test_ask_sends_filter_dict_when_given(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json={"query": "q", "answer": "a", "citations": []})

        client.ask("q", filter_dict={"severity": "high", "tags": ["x"]})

        body = json.loads(httpx_mock.get_request().content)
        assert body["filter_dict"] == {"severity": "high", "tags": ["x"]}

    def test_search_sends_filter_dict_when_given(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json={"query": "q", "results": []})

        client.search("q", filter_dict={"source_type": "playbook"})

        body = json.loads(httpx_mock.get_request().content)
        assert body["filter_dict"] == {"source_type": "playbook"}

    @pytest.mark.parametrize("empty", [None, {}])
    def test_empty_filters_leave_the_body_untouched(
        self, client, httpx_mock, empty
    ) -> None:
        """The desktop GUI never passes filters; its requests must not change."""
        httpx_mock.add_response(json={"query": "q", "answer": "a", "citations": []})
        httpx_mock.add_response(json={"query": "q", "results": []})

        client.ask("q", top_k=7, use_cache=False, filter_dict=empty)
        client.search("q", top_k=3, filter_dict=empty)

        ask_req, search_req = httpx_mock.get_requests()
        assert json.loads(ask_req.content) == {
            "query": "q",
            "top_k": 7,
            "use_cache": False,
        }
        assert json.loads(search_req.content) == {"query": "q", "top_k": 3}


class TestSessionIdHeader:
    def test_sent_when_configured(self, httpx_mock) -> None:
        httpx_mock.add_response(json={"query": "q", "results": []})
        c = GrimoireClient(GuiConfig(base_url=BASE, session_id="a1b2c3d4e5f6"))
        try:
            c.search("q")
        finally:
            c.close()

        assert httpx_mock.get_request().headers["X-Session-Id"] == "a1b2c3d4e5f6"

    def test_absent_when_not_configured(self, client, httpx_mock) -> None:
        """The desktop GUI does not opt in yet; its requests must not change."""
        httpx_mock.add_response(json={"query": "q", "results": []})

        client.search("q")

        assert "X-Session-Id" not in httpx_mock.get_request().headers

    def test_sent_on_every_endpoint_including_health(self, httpx_mock) -> None:
        httpx_mock.add_response(url=f"{BASE}/health", json={})
        httpx_mock.add_response(json={"documents": [], "total": 0})
        c = GrimoireClient(GuiConfig(base_url=BASE, session_id="sess-1"))
        try:
            c.health()
            c.list_documents()
        finally:
            c.close()

        assert [r.headers.get("X-Session-Id") for r in httpx_mock.get_requests()] == [
            "sess-1",
            "sess-1",
        ]

    def test_coexists_with_the_api_key_header(self, httpx_mock) -> None:
        httpx_mock.add_response(json={"query": "q", "results": []})
        c = GrimoireClient(
            GuiConfig(base_url=BASE, api_key="grim_agt_test", session_id="sess-1")
        )
        try:
            c.search("q")
        finally:
            c.close()

        headers = httpx_mock.get_request().headers
        assert headers["X-API-Key"] == "grim_agt_test"
        assert headers["X-Session-Id"] == "sess-1"
