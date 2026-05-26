"""Integration tests for the live Grimoire API deployed on Hetzner.

These tests hit the real server at http://grimoire.cybercrone.com:8001.
Set GRIMOIRE_TEST_API_KEY before running.

Usage:
    export GRIMOIRE_TEST_API_KEY=grim_agt_xxx...
    uv run pytest tests/integration/test_live_api.py -v

Use -m 'not slow' to skip slow LLM-dependent tests.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.getenv("GRIMOIRE_TEST_API_BASE", "http://grimoire.cybercrone.com:8001")
API_KEY = os.getenv("GRIMOIRE_TEST_API_KEY", "")

pytestmark = pytest.mark.live


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    if not API_KEY:
        pytest.skip("GRIMOIRE_TEST_API_KEY not set")
    return httpx.Client(
        base_url=BASE_URL,
        headers={"X-API-Key": API_KEY},
        timeout=30,
    )


# =============================================================================
# 1. Health & connectivity
# =============================================================================


class TestHealth:
    def test_health(self, client: httpx.Client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"

    def test_openapi_schema(self, client: httpx.Client):
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert schema.get("info", {}).get("title") == "Grimoire"
        assert "/api/v1/ingest/file" in schema.get("paths", {})


# =============================================================================
# 2. Auth / API key
# =============================================================================


class TestAuth:
    def test_key_info(self, client: httpx.Client):
        resp = client.get("/api/v1/keys/me")
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert "tier" in data
        assert data["tier"] in ("agt", "dvl", "rdl")

    def test_missing_key(self):
        no_key = httpx.Client(base_url=BASE_URL, timeout=10)
        resp = no_key.get("/health")
        assert resp.status_code == 200  # health is open
        resp = no_key.get("/api/v1/documents")
        assert resp.status_code == 401

    def test_invalid_key(self):
        bad = httpx.Client(
            base_url=BASE_URL,
            headers={"X-API-Key": "grim_agt_invalid_key"},
            timeout=10,
        )
        resp = bad.get("/api/v1/keys/me")
        assert resp.status_code == 401


# =============================================================================
# 3. Documents
# =============================================================================


class TestDocuments:
    def test_list_documents(self, client: httpx.Client):
        resp = client.get("/api/v1/documents", params={"limit": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert "documents" in data
        assert "total" in data
        assert isinstance(data["documents"], list)

    def test_list_pagination(self, client: httpx.Client):
        resp = client.get("/api/v1/documents", params={"offset": 0, "limit": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["documents"]) <= 2

    def test_list_security_filters(self, client: httpx.Client):
        """Security domain columns are filterable."""
        for param, value in (
            ("source_type", "sigma_rule"),
            ("source_type", "mitre_attack"),
            ("source_type", "nvd_cve"),
            ("severity", "high"),
            ("cve_id", "CVE-2024-0001"),
            ("mitre_technique_id", "T1059"),
        ):
            resp = client.get("/api/v1/documents", params={param: value, "limit": 1})
            assert resp.status_code == 200, f"Filter {param}={value} failed"
            data = resp.json()
            assert "documents" in data

    def test_document_detail_404(self, client: httpx.Client):
        resp = client.get("/api/v1/documents/nonexistent-id")
        assert resp.status_code == 404


# =============================================================================
# 4. Query / Search
# =============================================================================


class TestQuery:
    SLOW = pytest.mark.slow

    @SLOW
    def test_ask_question(self, client: httpx.Client):
        resp = client.post(
            "/api/v1/query/ask",
            json={"query": "What is a sigma rule?", "top_k": 3},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data
        assert "citations" in data
        assert isinstance(data["citations"], list)

    @SLOW
    def test_ask_with_security_filter(self, client: httpx.Client):
        resp = client.post(
            "/api/v1/query/ask",
            json={
                "query": "How do attackers use PowerShell?",
                "top_k": 5,
                "filter_dict": {"source_type": "mitre_attack"},
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "answer" in data

    def test_search_basic(self, client: httpx.Client):
        resp = client.post(
            "/api/v1/query/search",
            json={"query": "lateral movement", "top_k": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert isinstance(data["results"], list)
        assert "total_results" in data

    def test_search_security_filters(self, client: httpx.Client):
        for qs, body in (
            ({"severity": "high"}, {"query": "ransomware", "top_k": 3}),
            ({"tactic": "initial-access"}, {"query": "phishing", "top_k": 3}),
            ({"technique": "T1566"}, {"query": "spear phishing", "top_k": 3}),
        ):
            resp = client.post("/api/v1/query/search", params=qs, json=body)
            assert resp.status_code == 200, f"qs={qs} failed"


# =============================================================================
# 5. Categories
# =============================================================================


class TestCategories:
    def test_list_categories(self, client: httpx.Client):
        resp = client.get("/api/v1/categories")
        assert resp.status_code == 200
        data = resp.json()
        assert "categories" in data
        assert "total" in data


# =============================================================================
# 6. Ingest
# =============================================================================


class TestIngest:
    def test_ingest_file_path_traversal(self, client: httpx.Client):
        resp = client.post(
            "/api/v1/ingest/file",
            json={"file_path": "/etc/passwd"},
        )
        assert resp.status_code == 403

    def test_ingest_file_not_found(self, client: httpx.Client):
        resp = client.post(
            "/api/v1/ingest/file",
            json={"file_path": "/tmp/does_not_exist_12345.txt"},
        )
        assert resp.status_code == 404

    def test_ingest_directory_validation(self, client: httpx.Client):
        resp = client.post(
            "/api/v1/ingest/directory",
            json={"directory": "/etc", "recursive": True},
        )
        assert resp.status_code == 403

        resp = client.post(
            "/api/v1/ingest/directory",
            json={"directory": "/tmp/nonexistent_dir_12345", "recursive": True},
        )
        assert resp.status_code == 404


# =============================================================================
# 7. Generate
# =============================================================================


class TestGenerate:
    SLOW = pytest.mark.slow

    @SLOW
    def test_generate_summary(self, client: httpx.Client):
        """Requires at least one document in the DB."""
        # Grab a doc id first
        list_resp = client.get("/api/v1/documents", params={"limit": 1})
        if list_resp.status_code != 200:
            pytest.skip("Could not list documents")
        docs = list_resp.json().get("documents", [])
        if not docs:
            pytest.skip("No documents available for generation test")
        doc_id = docs[0]["id"]

        resp = client.post(
            "/api/v1/generate",
            json={
                "document_ids": [doc_id],
                "content_type": "summary",
                "style": "concise",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        assert data.get("content_type") == "summary"

    def test_generate_invalid_type(self, client: httpx.Client):
        resp = client.post(
            "/api/v1/generate",
            json={
                "document_ids": ["doc-1"],
                "content_type": "invalid_type",
            },
        )
        assert resp.status_code == 400

    def test_generate_missing_query_for_extract(self, client: httpx.Client):
        resp = client.post(
            "/api/v1/generate",
            json={
                "document_ids": ["doc-1"],
                "content_type": "extract",
            },
        )
        assert resp.status_code == 400


# =============================================================================
# 8. Watch (availability only — no active watcher on API mode)
# =============================================================================


class TestWatch:
    def test_watch_not_initialized(self, client: httpx.Client):
        resp = client.post(
            "/api/v1/watch/start",
            json={"path": "/tmp", "backend": "local"},
        )
        assert resp.status_code == 503

    def test_watch_status(self, client: httpx.Client):
        resp = client.get("/api/v1/watch/status")
        # Status may be 200 with empty state or 503 depending on impl
        assert resp.status_code in (200, 503)
