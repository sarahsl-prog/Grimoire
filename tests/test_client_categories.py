"""Category and tag methods on the shared client."""

from __future__ import annotations

import json

import pytest

from grimoire.client.client import GrimoireClient
from grimoire.client.config import GuiConfig
from grimoire.client.errors import RequestRejected

BASE = "http://testapi:8001"
CATEGORY = {
    "id": "cat-1",
    "name": "Alpha",
    "slug": "alpha",
    "description": "",
    "parent_id": None,
    "color": "#3498db",
    "document_count": 4,
}


@pytest.fixture
def client():
    c = GrimoireClient(GuiConfig(base_url=BASE, api_key="grim_dvl_test"))
    yield c
    c.close()


class TestListCategories:
    def test_parses_categories_with_their_counts(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json={"categories": [CATEGORY], "total": 1})

        result = client.list_categories()

        request = httpx_mock.get_request()
        assert (request.method, request.url.path) == ("GET", "/api/v1/categories")
        assert result.total == 1
        assert result.categories[0].name == "Alpha"
        assert result.categories[0].document_count == 4


class TestCreateCategory:
    def test_sends_only_what_was_given(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json=CATEGORY, status_code=201)

        created = client.create_category("Alpha")

        request = httpx_mock.get_request()
        assert (request.method, request.url.path) == ("POST", "/api/v1/categories")
        assert json.loads(request.content) == {"name": "Alpha"}
        assert created.slug == "alpha"

    def test_sends_description_and_parent(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json=CATEGORY, status_code=201)

        client.create_category("Alpha", description="Things", parent_slug="root")

        assert json.loads(httpx_mock.get_request().content) == {
            "name": "Alpha",
            "description": "Things",
            "parent_slug": "root",
        }

    def test_trims_the_name(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json=CATEGORY, status_code=201)

        client.create_category("  Alpha  ")

        assert json.loads(httpx_mock.get_request().content)["name"] == "Alpha"

    @pytest.mark.parametrize("name", ["", "   ", "x" * 101])
    def test_a_name_the_server_would_refuse_never_leaves_the_client(
        self, client, httpx_mock, name: str
    ) -> None:
        with pytest.raises(RequestRejected):
            client.create_category(name)

        assert httpx_mock.get_requests() == []

    def test_a_read_key_is_told_so_in_the_servers_words(
        self, client, httpx_mock
    ) -> None:
        httpx_mock.add_response(
            status_code=403,
            json={"detail": "This operation requires an API key of tier 'dvl'."},
        )

        with pytest.raises(RequestRejected, match="tier 'dvl'"):
            client.create_category("Alpha")


class TestTagging:
    @pytest.mark.parametrize(
        ("method", "call"),
        [("PUT", "tag_document"), ("DELETE", "untag_document")],
    )
    def test_uses_the_nested_tag_url(self, client, httpx_mock, method, call) -> None:
        httpx_mock.add_response(status_code=204)

        assert getattr(client, call)("doc-1", "cat-1") is None

        request = httpx_mock.get_request()
        assert (request.method, request.url.path) == (
            method,
            "/api/v1/documents/doc-1/tags/cat-1",
        )

    @pytest.mark.parametrize("call", ["tag_document", "untag_document"])
    def test_ids_cannot_add_path_segments(self, client, httpx_mock, call) -> None:
        httpx_mock.add_response(status_code=204)

        getattr(client, call)("a/b", "c?d=1")

        # quote(safe="") keeps "/" and "?" inside one path segment each.
        assert httpx_mock.get_request().url.raw_path.decode() == (
            "/api/v1/documents/a%2Fb/tags/c%3Fd%3D1"
        )

    @pytest.mark.parametrize("call", ["tag_document", "untag_document"])
    @pytest.mark.parametrize("ids", [("", "cat-1"), ("doc-1", "  ")])
    def test_a_blank_id_is_rejected_locally(
        self, client, httpx_mock, call, ids
    ) -> None:
        with pytest.raises(RequestRejected):
            getattr(client, call)(*ids)

        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize("call", ["tag_document", "untag_document"])
    def test_a_404_names_what_was_not_found(self, client, httpx_mock, call) -> None:
        httpx_mock.add_response(
            status_code=404, json={"detail": "Category x not found"}
        )

        with pytest.raises(RequestRejected, match="Category x not found"):
            getattr(client, call)("doc-1", "x")
