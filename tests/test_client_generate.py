"""``GrimoireClient.generate``: content generation from documents."""

from __future__ import annotations

import json

import pytest

from grimoire.client.client import GrimoireClient
from grimoire.client.config import GuiConfig
from grimoire.client.errors import RequestRejected

BASE = "http://testapi:8001"
GENERATED = {
    "content": "A short summary.",
    "content_type": "summary",
    "document_ids": ["doc-1"],
    "model_used": "llama3.2",
    "cached": False,
    "generation_id": "gen-1",
    "duration_ms": 4200,
}


@pytest.fixture
def client():
    c = GrimoireClient(GuiConfig(base_url=BASE, api_key="grim_dvl_test"))
    yield c
    c.close()


def _body(httpx_mock) -> dict:
    return json.loads(httpx_mock.get_request().content)


class TestRequest:
    def test_posts_the_ids_and_the_type(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json=GENERATED)

        result = client.generate(["doc-1"], "summary")

        request = httpx_mock.get_request()
        assert (request.method, request.url.path) == ("POST", "/api/v1/generate")
        assert _body(httpx_mock) == {
            "document_ids": ["doc-1"],
            "content_type": "summary",
        }
        assert result.content == "A short summary."
        assert result.model_used == "llama3.2"
        assert result.cached is False

    def test_sends_only_the_options_that_were_given(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json=GENERATED)

        client.generate(["doc-1"], "flash_card", count=5)

        assert _body(httpx_mock) == {
            "document_ids": ["doc-1"],
            "content_type": "flash_card",
            "count": 5,
        }

    def test_sends_a_trimmed_query_for_an_extract(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json=GENERATED)

        client.generate(["doc-1"], "extract", query="  CVE ids  ")

        assert _body(httpx_mock)["query"] == "CVE ids"

    def test_waits_for_the_long_timeout(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json=GENERATED)

        client.generate(["doc-1"], "summary")

        # An LLM can take minutes; the 30 second default would cut it off.
        assert httpx_mock.get_request().extensions["timeout"]["read"] == 300.0


class TestRefusedLocally:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"document_ids": [], "content_type": "summary"},
            {"document_ids": ["  "], "content_type": "summary"},
            {"document_ids": ["doc-1"], "content_type": "poem"},
            {"document_ids": ["doc-1"], "content_type": ""},
            {"document_ids": ["doc-1"], "content_type": "extract"},
            {"document_ids": ["doc-1"], "content_type": "extract", "query": "  "},
            {"document_ids": ["doc-1"], "content_type": "flash_card", "count": 0},
            {"document_ids": ["doc-1"], "content_type": "flash_card", "count": 101},
        ],
    )
    def test_a_request_the_server_would_refuse_never_leaves(
        self, client, httpx_mock, kwargs
    ) -> None:
        ids = kwargs.pop("document_ids")
        content_type = kwargs.pop("content_type")

        with pytest.raises(RequestRejected):
            client.generate(ids, content_type, **kwargs)

        assert httpx_mock.get_requests() == []


class TestServerErrors:
    def test_a_read_key_is_told_so_in_the_servers_words(
        self, client, httpx_mock
    ) -> None:
        httpx_mock.add_response(
            status_code=403,
            json={"detail": "This operation requires an API key of tier 'dvl'."},
        )

        with pytest.raises(RequestRejected, match="tier 'dvl'"):
            client.generate(["doc-1"], "summary")

    def test_a_missing_document_is_a_not_found(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            status_code=404, json={"detail": "Document x not found"}
        )

        with pytest.raises(RequestRejected, match="Document x not found"):
            client.generate(["x"], "summary")
