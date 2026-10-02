"""Textual terminal client for Grimoire.

The TUI is a thin HTTP client over the REST API, like the desktop GUI.  Keep
this package import-light: nothing under it may import the ingestion or query
pipeline, and the package ``__init__`` must not import Textual, so that
``grimoire --tui`` and ``grimoire-tui --version`` stay fast and can print an
install hint when the ``tui`` extra is missing.
"""
