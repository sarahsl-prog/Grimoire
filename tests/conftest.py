"""Pytest configuration and shared fixtures.

This module contains shared pytest fixtures and configuration for the Grimoire
test suite. Fixtures are organized by category: database, cache, and common utilities.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

# =============================================================================
# Event Loop Configuration
# =============================================================================


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# =============================================================================
# Path Fixtures
# =============================================================================


@pytest.fixture
def temp_directory() -> Generator[Path, None, None]:
    """Provide a temporary directory for tests.

    Yields:
        Path: Temporary directory path that is cleaned up after the test.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir)


@pytest.fixture
def sample_config_path(temp_directory: Path) -> Path:
    """Create a sample configuration file for testing.

    Args:
        temp_directory: Pytest fixture providing a temporary directory.

    Returns:
        Path: Path to a sample .env configuration file.
    """
    config_path = temp_directory / ".env"
    config_content = """
POSTGRES_USER=test_user
POSTGRES_PASSWORD=test_password
POSTGRES_DB=test_db
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
REDIS_HOST=localhost
REDIS_PORT=6379
CHROMADB_HOST=localhost
CHROMADB_PORT=8000
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2
"""
    config_path.write_text(config_content)
    return config_path


# =============================================================================
# Configuration Fixtures
# =============================================================================


@pytest.fixture
def mock_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set mock environment variables for testing.

    This fixture sets environment variables needed for configuration testing
    without requiring actual external services.
    """
    env_vars = {
        "POSTGRES_USER": "test_user",
        "POSTGRES_PASSWORD": "test_password",
        "POSTGRES_DB": "test_db",
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PORT": "5432",
        "REDIS_HOST": "localhost",
        "REDIS_PORT": "6379",
        "CHROMADB_HOST": "localhost",
        "CHROMADB_PORT": "8000",
        "OLLAMA_URL": "http://localhost:11434",
        "OLLAMA_MODEL": "llama3.2",
        "LOG_LEVEL": "DEBUG",
        "EMBEDDING_MODEL": "sentence-transformers/all-MiniLM-L6-v2",
    }
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)


# =============================================================================
# Async Fixtures
# =============================================================================


@pytest_asyncio.fixture
async def async_context() -> AsyncGenerator[dict[str, Any], None]:
    """Provide an async context for testing async code.

    Yields:
        dict: Context dictionary that can be used to share state in async tests.
    """
    context: dict[str, Any] = {}
    try:
        yield context
    finally:
        # Cleanup
        context.clear()
