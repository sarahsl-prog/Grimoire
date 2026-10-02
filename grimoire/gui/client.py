"""Compatibility shim: the client now lives in ``grimoire.client.client``."""

from grimoire.client.client import GrimoireClient

__all__ = ["GrimoireClient"]
