"""Compatibility shim: the config now lives in ``grimoire.client.config``."""

from grimoire.client.config import (
    DEFAULT_BASE_URL,
    ENV_API_KEY,
    ENV_BASE_URL,
    SUPPORTED_EXTENSIONS,
    GuiConfig,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "SUPPORTED_EXTENSIONS",
    "GuiConfig",
]
