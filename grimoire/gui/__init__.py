"""PySide6 desktop client for Grimoire.

The GUI is a thin HTTP client over the REST API.  Nothing in this package
imports the ingestion or query pipeline, so the GUI process never loads
torch, Docling, or ChromaDB.
"""
