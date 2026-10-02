"""HTTP client for the Grimoire REST API, shared by every front end.

Owned by neither the desktop GUI nor the terminal UI.  Nothing here imports Qt,
Textual, or the ingestion/query pipeline, so any client can depend on it
without loading torch, Docling, or ChromaDB.

``ClientConfig`` and ``ClientError`` are the neutral names for ``GuiConfig`` and
``GuiError``: same objects, kept under both names so the GUI did not have to be
renamed in the same change that moved the code.
"""

from __future__ import annotations

from grimoire.client.client import GrimoireClient
from grimoire.client.config import GuiConfig
from grimoire.client.errors import GuiError

ClientConfig = GuiConfig
ClientError = GuiError

__all__ = ["ClientConfig", "ClientError", "GrimoireClient", "GuiConfig", "GuiError"]
