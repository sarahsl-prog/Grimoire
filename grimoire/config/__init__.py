"""Configuration management for Grimoire.

This module provides centralized configuration management using Pydantic Settings.
Settings can be loaded from environment variables, .env files, and YAML config files.

Quick Start:
    >>> from grimoire.config import settings
    >>> print(settings.llm.model)
    >>> print(settings.database.url)

Reload Configuration:
    >>> from grimoire.config import reload_settings
    >>> settings = reload_settings()

Access via get_settings (for dependency injection):
    >>> from grimoire.config import get_settings
    >>> settings = get_settings()
"""

from grimoire.config.settings import (
    # Main settings class and functions
    GrimoireSettings,
    get_settings,
    reload_settings,
    settings,
    # Configuration sections
    LLMConfig,
    EmbeddingsConfig,
    DatabaseConfig,
    VectorStoreConfig,
    VectorStoreChromaConfig,
    VectorStoreQdrantConfig,
    LoggingConfig,
    CeleryConfig,
    RedisConfig,
    QueryConfig,
    CacheConfig,
    CloudConfig,
    CloudGoogleConfig,
    CloudOnedriveConfig,
    WatchConfig,
    ObservabilityConfig,
    ChunkingConfig,
    ChunkingSemanticConfig,
    ChunkingMarkdownConfig,
    ProcessingConfig,
    APIConfig,
    WikiConfig,
    EmbeddingIndexConfig,
    # Enums
    LogLevel,
    VectorStoreType,
    ChunkingStrategy,
    CacheStorage,
    EmbeddingDevice,
    DedupStrategy,
    # Custom sources
    YamlConfigSource,
)

__all__ = [
    # Main settings
    "GrimoireSettings",
    "get_settings",
    "reload_settings",
    "settings",
    # Config models
    "LLMConfig",
    "EmbeddingsConfig",
    "DatabaseConfig",
    "VectorStoreConfig",
    "VectorStoreChromaConfig",
    "VectorStoreQdrantConfig",
    "LoggingConfig",
    "CeleryConfig",
    "RedisConfig",
    "QueryConfig",
    "CacheConfig",
    "CloudConfig",
    "CloudGoogleConfig",
    "CloudOnedriveConfig",
    "WatchConfig",
    "ObservabilityConfig",
    "ChunkingConfig",
    "ChunkingSemanticConfig",
    "ChunkingMarkdownConfig",
    "ProcessingConfig",
    "APIConfig",
    "WikiConfig",
    "EmbeddingIndexConfig",
    # Enums
    "LogLevel",
    "VectorStoreType",
    "ChunkingStrategy",
    "CacheStorage",
    "EmbeddingDevice",
    "DedupStrategy",
    # Sources
    "YamlConfigSource",
]
