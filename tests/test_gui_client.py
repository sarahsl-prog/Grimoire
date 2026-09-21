"""Tests for the GUI's HTTP client and its error mapping.

No Qt here either — GrimoireClient is plain httpx, so these run without a
display server.
"""

from __future__ import annotations

import httpx
import pytest

from grimoire.gui.client import GrimoireClient
from grimoire.gui.config import GuiConfig
from grimoire.gui.errors import (
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
