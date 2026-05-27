"""Integration test configuration."""

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: hits the real deployed API")
    config.addinivalue_line("markers", "performance: performance and load tests")
    config.addinivalue_line("markers", "stress: heavy load — use sparingly on production")
    config.addinivalue_line("markers", "slow: tests that depend on LLM / embedding (expensive)")
