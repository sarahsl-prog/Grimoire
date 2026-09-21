"""Configuration for the Grimoire desktop client.

Deliberately free of Qt and of any Grimoire pipeline import, so it can be
constructed and tested without a display server or a heavyweight dependency
tree.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace

DEFAULT_BASE_URL = "http://localhost:8001"

ENV_BASE_URL = "GRIMOIRE_API_URL"
ENV_API_KEY = "GRIMOIRE_API_KEY"

# Mirrors DocumentParser.SUPPORTED_EXTENSIONS.  Duplicated rather than
# imported because grimoire.core.parser imports Docling at module scope, and
# the GUI must not pay that cost to filter a drag-and-drop.  tests/
# test_gui_config.py asserts the two stay identical.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".docx",
        ".doc",
        ".pptx",
        ".ppt",
        ".xlsx",
        ".xls",
        ".html",
        ".htm",
        ".md",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
        ".tiff",
        ".tif",
        ".gif",
        ".bmp",
        ".webp",
        ".json",
        ".yaml",
        ".yml",
    }
)


@dataclass(frozen=True)
class GuiConfig:
    """Everything the client needs to talk to a Grimoire API.

    Attributes:
        base_url: API root, without a trailing slash.
        api_key: Value sent as the X-API-Key header, or None when unset.
        connect_timeout: Seconds to wait for a connection.
        read_timeout: Seconds to wait for a fast endpoint's response.
        long_read_timeout: Seconds to wait for /query/ask and uploads, which
            are bounded by LLM generation and document parsing rather than
            by the network.
        max_upload_bytes: Client-side cap, mirroring the server's default so
            an oversized file is rejected before it is sent.
    """

    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    long_read_timeout: float = 300.0
    max_upload_bytes: int = 100 * 1024 * 1024

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GuiConfig:
        """Build a config from the environment.

        Args:
            env: Mapping to read instead of os.environ.  Tests pass an
                explicit dict rather than mutating the process environment.

        Returns:
            A GuiConfig.  Missing values fall back to the defaults; a blank
            or whitespace-only key counts as absent.
        """
        source = os.environ if env is None else env
        raw_url = source.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL
        raw_key = source.get(ENV_API_KEY, "").strip()
        return cls(base_url=raw_url.rstrip("/"), api_key=raw_key or None)

    def with_api_key(self, api_key: str) -> GuiConfig:
        """Return a copy carrying a different key.

        The GUI holds a pasted key in memory only; nothing here writes it to
        disk.
        """
        cleaned = api_key.strip()
        return replace(self, api_key=cleaned or None)

    @property
    def is_configured(self) -> bool:
        """Whether the client has a key to authenticate with."""
        return bool(self.api_key)
