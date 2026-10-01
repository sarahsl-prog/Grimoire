"""Entry point for the Grimoire terminal client.

Run with ``grimoire-tui`` after ``uv sync --extra tui``.  ``grimoire --tui``
and ``grimoire tui`` are shortcuts that call :func:`main`.

The order of operations below matters:

1. Parse arguments (cheap, and ``--help`` / ``--version`` never touch Textual).
2. Check Textual is installed, *before* any other grimoire import, so a
   missing extra produces a one-line hint instead of a traceback.
3. Configure file-only logging, so nothing reaches the terminal once the UI
   owns it.
4. Build and validate the config, then the client, then run the app.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import sys
from collections.abc import Sequence
from urllib.parse import urlsplit
from uuid import uuid4

INSTALL_HINT = "Textual is not installed. Run: uv sync --extra tui"


def _build_parser() -> argparse.ArgumentParser:
    # argparse rather than Click: this keeps `grimoire-tui` startup free of the
    # CLI group, whose callback validates server settings and logs to stderr.
    from grimoire import __version__

    parser = argparse.ArgumentParser(
        prog="grimoire-tui",
        description="Terminal client for a running Grimoire API server.",
        epilog=(
            "Environment: GRIMOIRE_API_URL (default http://localhost:8001) and "
            "GRIMOIRE_API_KEY. The API key is deliberately not a command-line "
            "option, because command-line arguments leak into process listings "
            "and shell history."
        ),
    )
    parser.add_argument(
        "--url",
        help="Grimoire API base URL. Overrides GRIMOIRE_API_URL.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Write DEBUG-level records (and HTTP request lines) to the log file.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def _exit_code(exc: SystemExit) -> int:
    """Normalise argparse's ``SystemExit`` payload (None, int, or message)."""
    if exc.code is None:
        return 0
    return exc.code if isinstance(exc.code, int) else 1


def _is_valid_base_url(url: str) -> bool:
    """Whether ``url`` is a usable ``http(s)://host[:port]`` API root.

    httpx accepts almost any string as ``base_url`` at construction and only
    fails on the first request, which in a TUI would mean launching a UI whose
    every action reports "cannot reach the API".  Reject it up front instead.
    """
    if not url or any(ch.isspace() for ch in url):
        return False
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a non-numeric or out-of-range port
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def _display_url(url: str) -> str:
    """The URL without any ``user:pass@`` part, safe to write to a log."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return f"{parts.scheme}://{host}"


def main(argv: Sequence[str] | None = None) -> int:
    """Start the TUI.

    Args:
        argv: Arguments after the program name.  ``None`` reads ``sys.argv``.
            Taking it as a parameter lets the Click shortcuts and the tests
            call this directly.

    Returns:
        A process exit code: 0 on a normal exit, the app's own return code if
        it set one, 1 for a startup problem (missing Textual, malformed URL),
        or argparse's code for bad arguments.
    """
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:  # --help, --version, and usage errors
        return _exit_code(exc)

    try:
        importlib.import_module("textual")
    except ImportError:
        print(INSTALL_HINT, file=sys.stderr)
        return 1

    from loguru import logger

    from grimoire.tui.logsetup import configure_tui_logging

    session_id = uuid4().hex[:12]
    # Bound once so every record from this launch can be isolated in the log,
    # and sent to the API so server-side records can be matched to it.
    log_path = configure_tui_logging(session_id, debug=args.debug)

    from grimoire.gui.client import GrimoireClient
    from grimoire.gui.config import GuiConfig
    from grimoire.gui.errors import ConnectionFailed

    config = GuiConfig.from_env()
    if args.url is not None:
        config = dataclasses.replace(config, base_url=args.url.rstrip("/"))
    if not _is_valid_base_url(config.base_url):
        # The value is not echoed: it may come from an environment variable
        # that carries credentials.
        print(
            "Invalid Grimoire API URL. Expected http://host[:port] or "
            "https://host[:port] (from --url or GRIMOIRE_API_URL).",
            file=sys.stderr,
        )
        return 1
    config = dataclasses.replace(config, session_id=session_id)

    logger.info(
        f"Starting Grimoire TUI against {_display_url(config.base_url)} "
        f"(key {'set' if config.api_key else 'missing'})"
    )

    # Imported before the client exists so a failure here cannot leak one.
    try:
        from grimoire.tui.app import GrimoireApp
    except ImportError:
        logger.exception("Could not import the TUI application")
        where = f" See {log_path}." if log_path else ""
        print(f"Could not start the TUI: it failed to load.{where}", file=sys.stderr)
        return 1

    try:
        client = GrimoireClient(config)
    except ConnectionFailed as exc:
        print(exc.message, file=sys.stderr)
        return 1

    app = GrimoireApp(client, config, log_path)
    try:
        app.run()
    finally:
        client.close()
    return int(getattr(app, "return_code", 0) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
