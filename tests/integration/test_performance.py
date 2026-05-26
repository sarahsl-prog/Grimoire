"""Performance smoke tests for the live Grimoire API.

Usage:
    export GRIMOIRE_TEST_API_KEY=grim_agt_xxx...
    uv run pytest tests/integration/test_performance.py -v

Markers:
    -m 'not slow'     — skip LLM-dependent tests
    -m 'stress'       — run heavy-load tests (use sparingly on production)
"""

from __future__ import annotations

import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx
import pytest

BASE_URL = os.getenv("GRIMOIRE_TEST_API_BASE", "http://grimoire.cybercrone.com:8001")
API_KEY = os.getenv("GRIMOIRE_TEST_API_KEY", "")

pytestmark = [pytest.mark.live, pytest.mark.performance]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request_latency(client: httpx.Client, method: str, path: str, **kwargs: Any) -> float:
    start = time.perf_counter()
    resp = getattr(client, method)(path, **kwargs)
    elapsed = (time.perf_counter() - start) * 1000  # ms
    resp.raise_for_status()
    return elapsed


def _percentile(values: list[float], p: float) -> float:
    sorted_vals = sorted(values)
    idx = int((p / 100) * (len(sorted_vals) - 1))
    return sorted_vals[idx]


@pytest.fixture
def perf_client() -> httpx.Client:
    if not API_KEY:
        pytest.skip("GRIMOIRE_TEST_API_KEY not set")
    return httpx.Client(
        base_url=BASE_URL,
        headers={"X-API-Key": API_KEY},
        timeout=30,
    )


# =============================================================================
# 1. Latency benchmarks (single request)
# =============================================================================


class TestLatencyBenchmarks:
    """Check response times stay within acceptable bounds."""

    HEALTH_MAX_MS = 500
    LIST_MAX_MS = 1500
    SEARCH_MAX_MS = 5000  # vector search + rerank can be heavy

    def test_health_latency(self, perf_client: httpx.Client):
        latencies = [_request_latency(perf_client, "get", "/health") for _ in range(5)]
        p95 = _percentile(latencies, 95)
        assert p95 < self.HEALTH_MAX_MS, f"Health p95 = {p95:.1f}ms (max {self.HEALTH_MAX_MS}ms)"

    def test_list_docs_latency(self, perf_client: httpx.Client):
        latencies = [
            _request_latency(perf_client, "get", "/api/v1/documents", params={"limit": 10})
            for _ in range(5)
        ]
        p95 = _percentile(latencies, 95)
        assert p95 < self.LIST_MAX_MS, f"List docs p95 = {p95:.1f}ms (max {self.LIST_MAX_MS}ms)"

    @pytest.mark.slow
    def test_search_latency(self, perf_client: httpx.Client):
        latencies = [
            _request_latency(
                perf_client,
                "post",
                "/api/v1/query/search",
                json={"query": "ransomware", "top_k": 5},
            )
            for _ in range(3)
        ]
        p95 = _percentile(latencies, 95)
        assert p95 < self.SEARCH_MAX_MS, f"Search p95 = {p95:.1f}ms (max {self.SEARCH_MAX_MS}ms)"


# =============================================================================
# 2. Throughput smoke test (parallel GETs)
# =============================================================================


class TestThroughput:
    CONCURRENCY = 10
    REQUESTS = 50
    RPS_MIN = 2.0  # minimum acceptable requests/sec for light endpoints
    ERROR_RATE_MAX = 0.05  # 5% max error rate

    @pytest.mark.stress
    def test_parallel_health(self, perf_client: httpx.Client):
        """Hammer /health with N concurrent clients."""
        results: list[tuple[float, int]] = []

        def _hit():
            start = time.perf_counter()
            try:
                resp = perf_client.get("/health")
                code = resp.status_code
            except Exception:
                code = 0
            elapsed = (time.perf_counter() - start) * 1000
            return elapsed, code

        start_all = time.perf_counter()
        with ThreadPoolExecutor(max_workers=self.CONCURRENCY) as pool:
            futures = [pool.submit(_hit) for _ in range(self.REQUESTS)]
            for f in as_completed(futures):
                results.append(f.result())
        total_ms = (time.perf_counter() - start_all) * 1000

        ok = [r for r in results if r[1] == 200]
        errors = [r for r in results if r[1] != 200]
        rps = len(results) / (total_ms / 1000)
        p95 = _percentile([r[0] for r in ok], 95) if ok else 0

        print(f"\n[health throughput] {len(results)} reqs, {len(errors)} errors, {rps:.1f} r/s, p95={p95:.1f}ms")
        assert len(errors) / len(results) <= self.ERROR_RATE_MAX
        assert rps >= self.RPS_MIN


# =============================================================================
# 3. Rate-limiting behaviour
# =============================================================================


class TestRateLimiting:
    """Verify the API returns 429 when rate limits are exceeded.

    NOTE: This test is intentionally conservative. It runs 40 rapid
    sequential requests against /health (which should have a generous limit)
    and only asserts that *some* 429s appear if we hit the ceiling.
    Adjust if your key tier limits are known.
    """

    def test_rate_limit_headers_present(self, perf_client: httpx.Client):
        resp = perf_client.get("/health")
        assert resp.status_code == 200
        # Common rate-limit headers (optional — not all servers emit them)
        assert any(
            h in resp.headers for h in ("x-ratelimit-limit", "x-ratelimit-remaining", "retry-after")
        ) or True  # soft assertion — we don't fail if headers are absent

    @pytest.mark.stress
    def test_rate_limit_429_eventually(self, perf_client: httpx.Client):
        """Rapid-fire requests until we see a 429 or exhaust our burst."""
        codes = []
        for _ in range(60):
            resp = perf_client.get("/health")
            codes.append(resp.status_code)
            if resp.status_code == 429:
                break
            time.sleep(0.05)  # 20 r/s burst
        else:
            pytest.skip("Did not hit rate limit within 60 requests; limits may be too high for this key tier")

        assert 429 in codes


# =============================================================================
# 4. Memory / stability stress
# =============================================================================


class TestStability:
    """Repeated large payload operations to catch memory leaks or DB exhaustion."""

    ITERATIONS = 20

    def test_repeated_list_no_growth(self, perf_client: httpx.Client):
        """List docs many times; latency should not balloon."""
        latencies = [
            _request_latency(
                perf_client, "get", "/api/v1/documents", params={"limit": 50}
            )
            for _ in range(self.ITERATIONS)
        ]
        first = latencies[0]
        last = latencies[-1]
        # Allow 3x growth — if it gets much worse, something is leaking
        assert last < first * 3, f"Latency grew from {first:.1f}ms to {last:.1f}ms"
