"""Configuration for the Grimoire desktop client.

Deliberately free of Qt and of any Grimoire pipeline import, so it can be
constructed and tested without a display server or a heavyweight dependency
tree.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import dotenv_values
from loguru import logger

DEFAULT_BASE_URL = "http://localhost:8001"

ENV_BASE_URL = "GRIMOIRE_API_URL"
ENV_API_KEY = "GRIMOIRE_API_KEY"

#: Looked up in the current directory when no explicit path is given.
DEFAULT_DOTENV_PATH = Path(".env")

# The id travels as an HTTP header and ends up in server logs, so it is held to
# a conservative alphabet.  fullmatch, not `^...$`: `$` also matches before a
# trailing newline, which is exactly the byte a log-injection attempt needs.
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

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
        session_id: Per-launch identifier sent as the X-Session-Id header so
            server-side log records can be matched to this client's own log.
            None (the default) sends no header.  Must be 1-64 characters of
            ``A-Z a-z 0-9 _ -``; anything else raises ValueError.
    """

    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    long_read_timeout: float = 300.0
    max_upload_bytes: int = 100 * 1024 * 1024
    session_id: str | None = None

    def __post_init__(self) -> None:
        # Runs for dataclasses.replace() too, so there is no way to build a
        # config carrying an unvalidated id.
        if self.session_id is not None and not _SESSION_ID_PATTERN.fullmatch(
            self.session_id
        ):
            # Deliberately omits the value: it may be attacker-influenced and
            # this message can reach a log.
            raise ValueError(
                "session_id must be 1-64 characters of letters, digits, "
                "underscore or hyphen"
            )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        dotenv_path: Path | None = None,
    ) -> GuiConfig:
        """Build a config from the environment, then a ``.env`` file.

        Precedence, per key: a real environment variable, then the ``.env``
        file, then the default.  A blank or whitespace-only value counts as
        absent at every layer, so ``GRIMOIRE_API_KEY=`` cannot mask a key
        that is set further down.

        Only ``GRIMOIRE_API_URL`` and ``GRIMOIRE_API_KEY`` are taken from the
        file.  Nothing is exported into ``os.environ``, variables in the file
        are not interpolated, and no value is logged.

        Args:
            env: Mapping to read instead of ``os.environ``.  Tests pass an
                explicit dict rather than mutating the process environment.
            dotenv_path: The file to read.  When omitted, the process
                environment (``env is None``) also reads ``.env`` in the
                current directory, while an explicit ``env`` mapping reads no
                file at all, so a caller or test that supplies its own
                environment is never influenced by whichever directory it
                happens to run in.

        Returns:
            A GuiConfig.  Missing values fall back to the defaults.  A missing
            or unreadable file is not an error.
        """
        source = os.environ if env is None else env
        if dotenv_path is None and env is None:
            dotenv_path = DEFAULT_DOTENV_PATH
        from_file = _read_dotenv(dotenv_path) if dotenv_path is not None else {}

        def pick(name: str) -> str:
            return source.get(name, "").strip() or from_file.get(name, "").strip()

        raw_url = pick(ENV_BASE_URL) or DEFAULT_BASE_URL
        raw_key = pick(ENV_API_KEY)
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


def _read_dotenv(path: Path) -> dict[str, str]:
    """Return the client's two settings from ``path``, or ``{}`` on any problem.

    ``dotenv_values`` parses without touching ``os.environ``; interpolation is
    off so ``${VAR}`` in a value cannot pull in the process environment.  A
    file that is missing, a directory, unreadable or not valid UTF-8 simply
    contributes nothing: a client must still start from the environment alone.
    Only the outcome is logged, never a key or a value.
    """
    try:
        values = dotenv_values(path, interpolate=False, encoding="utf-8")
    except (OSError, ValueError) as exc:
        # The exception class only: its text can quote the offending bytes.
        logger.debug(f"Ignoring unreadable .env ({type(exc).__name__})")
        return {}
    return {
        name: value
        for name, value in values.items()
        if name in (ENV_BASE_URL, ENV_API_KEY) and value is not None
    }


#: Neutral name for front ends that are not the desktop GUI.
ClientConfig = GuiConfig
