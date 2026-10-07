"""Watcher methods on the shared client."""

from __future__ import annotations

import json

import pytest

from grimoire.client.client import GrimoireClient
from grimoire.client.config import GuiConfig
from grimoire.client.errors import RequestRejected, ServerError

BASE = "http://testapi:8001"
WATCH = {"watch_id": "w1", "path": "/tmp/docs", "backend": "local", "is_running": True}


@pytest.fixture
def client():
    c = GrimoireClient(GuiConfig(base_url=BASE, api_key="grim_dvl_test"))
    yield c
    c.close()


class TestWatchStatus:
    def test_parses_counters_and_watches(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            json={
                "active_watches": 1,
                "total_files_processed": 7,
                "total_files_failed": 2,
                "watches": [WATCH],
            }
        )

        result = client.watch_status()

        request = httpx_mock.get_request()
        assert (request.method, request.url.path) == ("GET", "/api/v1/watch/status")
        assert result.total_files_processed == 7
        assert result.watches[0].path == "/tmp/docs"

    def test_503_when_the_server_has_no_watcher(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            status_code=503, json={"detail": "Watcher not initialized."}
        )

        with pytest.raises(ServerError, match="not initialized"):
            client.watch_status()

    def test_a_proxys_html_503_stays_generic(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=503, text="<html>upstream down</html>")

        with pytest.raises(ServerError) as info:
            client.watch_status()

        assert "upstream" not in info.value.message


class TestStartWatch:
    def test_posts_path_and_recursive_flag(self, client, httpx_mock) -> None:
        httpx_mock.add_response(json=WATCH, status_code=201)

        started = client.start_watch("/tmp/docs", recursive=False)

        request = httpx_mock.get_request()
        assert (request.method, request.url.path) == ("POST", "/api/v1/watch/start")
        assert json.loads(request.content) == {"path": "/tmp/docs", "recursive": False}
        assert started.watch_id == "w1"

    def test_blank_path_is_rejected_locally(self, client, httpx_mock) -> None:
        with pytest.raises(RequestRejected):
            client.start_watch("   ")

        assert httpx_mock.get_requests() == []

    def test_a_refusal_is_reported(self, client, httpx_mock) -> None:
        httpx_mock.add_response(
            status_code=403, json={"detail": "Path is not in an allowed directory."}
        )

        with pytest.raises(RequestRejected):
            client.start_watch("/etc")


class TestStopWatch:
    def test_deletes_the_watch(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=204)

        assert client.stop_watch("w1") is None

        request = httpx_mock.get_request()
        assert (request.method, request.url.path) == ("DELETE", "/api/v1/watch/w1")

    def test_id_cannot_add_path_segments(self, client, httpx_mock) -> None:
        httpx_mock.add_response(status_code=204)

        client.stop_watch("a/b")

        assert httpx_mock.get_request().url.raw_path.decode() == "/api/v1/watch/a%2Fb"

    def test_blank_id_is_rejected_locally(self, client, httpx_mock) -> None:
        with pytest.raises(RequestRejected):
            client.stop_watch(" ")

        assert httpx_mock.get_requests() == []
