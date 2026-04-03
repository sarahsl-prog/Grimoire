"""Configuration management for Grimoire using Pydantic Settings.

This module provides a centralized configuration system that loads settings
from environment variables, .env files, and YAML configuration files.
All settings are validated using Pydantic v2 models.

Example:
    >>> from grimoire.config import settings
    >>> print(settings.database.url)
    >>> print(settings.llm.model)
"""

from __future__ import annotations

import enum
import os
from pathlib import Path
from typing import Any, Self

import yaml
from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# =============================================================================
# Enums
# =============================================================================


class LogLevel(str, enum.Enum):
    """Valid log levels for loguru."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class VectorStoreType(str, enum.Enum):
    """Supported vector store backends."""

    CHROMADB = "chromadb"
    QDRANT = "qdrant"


class ChunkingStrategy(str, enum.Enum):
    """Supported document chunking strategies."""

    SEMANTIC = "semantic"
    MARKDOWN = "markdown"
    RECURSIVE = "recursive"


class CacheStorage(str, enum.Enum):
    """Supported cache storage backends."""

    DISK = "disk"
    REDIS = "redis"


class EmbeddingDevice(str, enum.Enum):
    """Device options for embedding models."""

    AUTO = "auto"
    CUDA = "cuda"
    CPU = "cpu"
    MPS = "mps"


class DedupStrategy(str, enum.Enum):
    """Deduplication strategies."""

    HASH = "hash"
    CONTENT = "content"


# =============================================================================
# Nested Configuration Models
# =============================================================================


class LLMConfig(BaseModel):
    """LLM configuration for Ollama integration.

    Attributes:
        model: Ollama model name to use.
        url: Base URL for Ollama API.
        temperature: Sampling temperature (0.0 to 2.0).
        max_tokens: Maximum tokens in response.
        timeout: Request timeout in seconds.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(default="llama3.2", description="Ollama model name")
    url: str = Field(default="http://localhost:11434", description="Ollama base URL")
    temperature: float = Field(
        default=0.7, ge=0.0, le=2.0, description="Sampling temperature"
    )
    max_tokens: int = Field(
        default=4096, ge=1, le=128000, description="Maximum tokens per response"
    )
    timeout: int = Field(default=300, ge=1, description="Request timeout in seconds")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate URL format."""
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with http:// or https://: {v}")
        return v


class EmbeddingIndexConfig(BaseModel):
    """Per-index embedding configuration.

    Allows different embedding models for different document types.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Embedding model name for this index")


class EmbeddingsConfig(BaseModel):
    """Embedding model configuration.

    Attributes:
        model: Primary embedding model name.
        fallback_model: Fallback model if primary fails.
        device: Device to run embeddings on (auto/cuda/cpu/mps).
        batch_size: Batch size for embedding computation.
        indices: Per-index model overrides.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(
        default="sentence-transformers/all-mpnet-base-v2",
        description="Primary embedding model",
    )
    fallback_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        description="Fallback embedding model",
    )
    device: str = Field(default="auto", description="Device: auto, cuda, cpu, mps")
    batch_size: int = Field(default=32, ge=1, le=512, description="Batch size")
    indices: dict[str, EmbeddingIndexConfig] = Field(
        default_factory=dict, description="Per-index model overrides"
    )

    @field_validator("device")
    @classmethod
    def validate_device(cls, v: str) -> str:
        """Validate device option."""
        valid_devices = ["auto", "cuda", "cpu", "mps"]
        if v.lower() not in valid_devices:
            raise ValueError(f"Device must be one of {valid_devices}: {v}")
        return v.lower()

    @field_validator("model", "fallback_model")
    @classmethod
    def validate_model_name(cls, v: str) -> str:
        """Validate model name format."""
        if "/" not in v:
            raise ValueError(f"Model name must be in 'namespace/model' format: {v}")
        return v


class DatabaseConfig(BaseModel):
    """Database configuration.

    Attributes:
        url: PostgreSQL connection URL.
        dev_url: SQLite URL for development.
        echo: Enable SQL statement logging.
        pool_size: Connection pool size (PostgreSQL only).
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        default="postgresql+asyncpg://grimoire:changeme@localhost:5432/grimoire",
        description="PostgreSQL connection URL",
    )
    dev_url: str = Field(
        default="sqlite:///grimoire.db",
        description="SQLite URL for development",
    )
    echo: bool = Field(default=False, description="Log SQL statements")
    pool_size: int = Field(default=10, ge=1, le=100, description="Connection pool size")

    @field_validator("url", "dev_url")
    @classmethod
    def validate_db_url(cls, v: str) -> str:
        """Validate database URL format."""
        if not v.startswith(("postgresql://", "postgresql+asyncpg://", "sqlite://")):
            raise ValueError(
                f"Database URL must start with postgresql:// or sqlite://: {v}"
            )
        return v

    @field_validator("url")
    @classmethod
    def validate_postgres_url(cls, v: str) -> str:
        """Validate PostgreSQL URL has required components."""
        if v.startswith(("postgresql://", "postgresql+asyncpg://")):
            # Basic validation - should have user:pass@host:port/dbname
            if "@" not in v:
                raise ValueError(
                    f"PostgreSQL URL should contain credentials (user:pass@host): {v}"
                )
        return v


class VectorStoreChromaConfig(BaseModel):
    """ChromaDB-specific configuration."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(default="./chroma_db", description="ChromaDB persistence path")
    collection_name: str = Field(
        default="documents", description="Default collection name"
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        """Ensure path is valid."""
        # Path will be expanded and validated during initialization
        return v


class VectorStoreQdrantConfig(BaseModel):
    """Qdrant-specific configuration."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(default="http://localhost:6333", description="Qdrant server URL")
    api_key: str | None = Field(default=None, description="API key for cloud Qdrant")
    collection_name: str = Field(
        default="documents", description="Default collection name"
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate URL format."""
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"Qdrant URL must start with http:// or https://: {v}")
        return v


class VectorStoreConfig(BaseModel):
    """Vector store configuration.

    Attributes:
        type: Backend type (chromadb or qdrant).
        chromadb: ChromaDB-specific settings.
        qdrant: Qdrant-specific settings.
        host: Optional host for remote ChromaDB.
        port: Optional port for remote ChromaDB.
    """

    model_config = ConfigDict(extra="forbid")

    type: VectorStoreType = Field(
        default=VectorStoreType.CHROMADB, description="Vector store backend type"
    )
    chromadb: VectorStoreChromaConfig = Field(
        default_factory=VectorStoreChromaConfig, description="ChromaDB settings"
    )
    qdrant: VectorStoreQdrantConfig = Field(
        default_factory=VectorStoreQdrantConfig, description="Qdrant settings"
    )
    host: str | None = Field(default=None, description="ChromaDB host (optional)")
    port: int | None = Field(
        default=None, ge=1, le=65535, description="ChromaDB port (optional)"
    )


class LoggingConfig(BaseModel):
    """Logging configuration using loguru.

    Attributes:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        format: Log message format string.
        rotation: Log rotation interval.
        retention: Log retention period.
        log_dir: Directory for log files.
        structured: Enable structured JSON logging.
        use_json: Enable JSON format output.
    """

    model_config = ConfigDict(extra="forbid")

    level: LogLevel = Field(default=LogLevel.INFO, description="Log level")
    format: str = Field(
        default="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        description="Log format string",
    )
    rotation: str = Field(default="1 week", description="Log rotation interval")
    retention: str = Field(default="1 month", description="Log retention period")
    log_dir: str = Field(default="./logs", description="Log directory path")
    structured: bool = Field(default=True, description="Enable structured logging")
    use_json: bool = Field(default=False, description="Output logs in JSON format")

    @field_validator("log_dir")
    @classmethod
    def validate_log_dir(cls, v: str) -> str:
        """Expand user and validate log directory path."""
        expanded = os.path.expanduser(v)
        return expanded

    @model_validator(mode="after")
    def create_log_dir(self) -> Self:
        """Ensure log directory exists."""
        log_path = Path(os.path.expanduser(self.log_dir))
        try:
            log_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Could not create log directory {log_path}: {e}")
        return self


class CeleryConfig(BaseModel):
    """Celery distributed task queue configuration.

    Attributes:
        broker_url: Redis URL for message broker.
        result_backend: Redis URL for result storage.
        task_serializer: Serialization format for tasks.
        accept_content: Accepted content types.
        result_serializer: Result serialization format.
        timezone: Timezone for task scheduling.
        enable_utc: Use UTC for all timestamps.
    """

    model_config = ConfigDict(extra="forbid")

    broker_url: str = Field(
        default="redis://localhost:6379/0", description="Redis broker URL"
    )
    result_backend: str = Field(
        default="redis://localhost:6379/1", description="Redis result backend URL"
    )
    task_serializer: str = Field(default="json", description="Task serializer")
    accept_content: list[str] = Field(
        default_factory=lambda: ["json"], description="Accepted content types"
    )
    result_serializer: str = Field(default="json", description="Result serializer")
    timezone: str = Field(default="UTC", description="Timezone for tasks")
    enable_utc: bool = Field(default=True, description="Enable UTC timestamps")

    @field_validator("broker_url", "result_backend")
    @classmethod
    def validate_redis_url(cls, v: str) -> str:
        """Validate Redis URL format."""
        if not v.startswith("redis://"):
            raise ValueError(f"Celery URL must be a redis:// URL: {v}")
        return v


class RedisConfig(BaseModel):
    """Redis configuration for caching and messaging.

    Attributes:
        host: Redis server hostname.
        port: Redis server port.
        password: Redis password (optional).
        db_cache: Database number for cache.
        url: Full Redis URL (optional, overrides host/port).
    """

    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="localhost", description="Redis hostname")
    port: int = Field(default=6379, ge=1, le=65535, description="Redis port")
    password: str | None = Field(default=None, description="Redis password")
    db_cache: int = Field(
        default=2, ge=0, le=15, description="Redis database for cache"
    )
    url: str | None = Field(default=None, description="Full Redis URL")

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate port is in valid range."""
        if not 1 <= v <= 65535:
            raise ValueError(f"Port must be between 1 and 65535: {v}")
        return v


class QueryConfig(BaseModel):
    """Query/RAG configuration.

    Attributes:
        default_top_k: Default number of results to return.
        rerank_top_k: Number of results to retrieve before reranking.
        rerank_model: Cross-encoder model for reranking.
        hybrid_alpha: Weight for vector vs full-text search (0.0-1.0).
        enable_citations: Include source citations in responses.
    """

    model_config = ConfigDict(extra="forbid")

    default_top_k: int = Field(
        default=10, ge=1, le=100, description="Default number of results"
    )
    rerank_top_k: int = Field(
        default=50, ge=1, le=500, description="Reranking pool size"
    )
    rerank_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        description="Reranking model",
    )
    hybrid_alpha: float = Field(
        default=0.7, ge=0.0, le=1.0, description="Vector vs FTS weight"
    )
    enable_citations: bool = Field(default=True, description="Enable source citations")


class CacheConfig(BaseModel):
    """Cache configuration.

    Attributes:
        embedding_ttl: TTL for embeddings in seconds.
        result_ttl: TTL for query results in seconds.
        generation_ttl: TTL for generated content in seconds.
        storage: Backend storage type (disk or redis).
        path: Path for disk cache.
    """

    model_config = ConfigDict(extra="forbid")

    embedding_ttl: int = Field(
        default=604800, ge=0, description="Embedding cache TTL (seconds)"
    )
    result_ttl: int = Field(
        default=86400, ge=0, description="Result cache TTL (seconds)"
    )
    generation_ttl: int = Field(
        default=2592000, ge=0, description="Generation cache TTL (seconds)"
    )
    storage: CacheStorage = Field(
        default=CacheStorage.DISK, description="Cache storage backend"
    )
    path: str = Field(default="./cache", description="Disk cache path")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        """Expand user in cache path."""
        return os.path.expanduser(v)


class CloudGoogleConfig(BaseModel):
    """Google Drive cloud storage configuration."""

    model_config = ConfigDict(extra="forbid")

    credentials_path: str = Field(
        default="~/.config/gcloud/credentials.json",
        description="Path to Google credentials",
    )
    token_store: str = Field(
        default="~/.config/grimoire/gdrive_tokens.json",
        description="Path to store OAuth tokens",
    )
    client_id: str | None = Field(default=None, description="OAuth client ID")
    client_secret: str | None = Field(default=None, description="OAuth client secret")

    @field_validator("credentials_path", "token_store")
    @classmethod
    def expand_paths(cls, v: str) -> str:
        """Expand user home directory."""
        return os.path.expanduser(v)


class CloudOnedriveConfig(BaseModel):
    """OneDrive cloud storage configuration."""

    model_config = ConfigDict(extra="forbid")

    client_id: str | None = Field(default=None, description="Microsoft Graph client ID")
    client_secret: str | None = Field(
        default=None, description="Microsoft Graph client secret"
    )
    token_store: str = Field(
        default="~/.config/grimoire/onedrive_tokens.json",
        description="Path to store OAuth tokens",
    )

    @field_validator("token_store")
    @classmethod
    def expand_path(cls, v: str) -> str:
        """Expand user home directory."""
        return os.path.expanduser(v)


class CloudConfig(BaseModel):
    """Cloud storage configuration."""

    model_config = ConfigDict(extra="forbid")

    google: CloudGoogleConfig = Field(
        default_factory=CloudGoogleConfig, description="Google Drive settings"
    )
    onedrive: CloudOnedriveConfig = Field(
        default_factory=CloudOnedriveConfig, description="OneDrive settings"
    )


class WatchConfig(BaseModel):
    """File watching configuration.

    Attributes:
        default_poll_interval: Polling interval for cloud storage (seconds).
        max_local_watches: Maximum number of local directory watchers.
        ignore_patterns: Glob patterns for files/directories to ignore.
    """

    model_config = ConfigDict(extra="forbid")

    default_poll_interval: int = Field(
        default=300, ge=10, description="Cloud polling interval (seconds)"
    )
    max_local_watches: int = Field(
        default=100, ge=1, description="Maximum local watchers"
    )
    ignore_patterns: list[str] = Field(
        default_factory=lambda: [
            "*.tmp",
            ".git/",
            "__pycache__/",
            ".DS_Store",
            "*.log",
        ],
        description="Patterns to ignore during file watching",
    )


class ObservabilityConfig(BaseModel):
    """Observability and monitoring configuration.

    Attributes:
        log_level: Log level for observability.
        structured_logs: Enable structured logging.
        tracing: Enable OpenTelemetry/LangSmith tracing.
        metrics: Enable Prometheus metrics export.
    """

    model_config = ConfigDict(extra="forbid")

    log_level: LogLevel = Field(
        default=LogLevel.INFO, description="Observability log level"
    )
    structured_logs: bool = Field(default=True, description="Enable structured logging")
    tracing: bool = Field(default=False, description="Enable distributed tracing")
    metrics: bool = Field(default=False, description="Enable Prometheus metrics")


class ChunkingSemanticConfig(BaseModel):
    """Semantic chunking-specific settings."""

    model_config = ConfigDict(extra="forbid")

    threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Cosine similarity threshold"
    )
    min_chunk_size: int = Field(
        default=100, ge=10, description="Minimum chunk size in characters"
    )


class ChunkingMarkdownConfig(BaseModel):
    """Markdown chunking-specific settings."""

    model_config = ConfigDict(extra="forbid")

    headers_to_split_on: list[str] = Field(
        default_factory=lambda: ["#", "##", "###"],
        description="Markdown headers to split on",
    )


class ChunkingConfig(BaseModel):
    """Document chunking configuration.

    Attributes:
        default_strategy: Default chunking strategy.
        chunk_size: Target chunk size in tokens.
        chunk_overlap: Overlap between chunks in tokens.
        semantic: Semantic chunking settings.
        markdown: Markdown chunking settings.
    """

    model_config = ConfigDict(extra="forbid")

    default_strategy: ChunkingStrategy = Field(
        default=ChunkingStrategy.SEMANTIC, description="Default chunking strategy"
    )
    chunk_size: int = Field(
        default=1000, ge=100, le=8000, description="Target chunk size (tokens)"
    )
    chunk_overlap: int = Field(
        default=200, ge=0, le=1000, description="Chunk overlap (tokens)"
    )
    semantic: ChunkingSemanticConfig = Field(
        default_factory=ChunkingSemanticConfig, description="Semantic chunking settings"
    )
    markdown: ChunkingMarkdownConfig = Field(
        default_factory=ChunkingMarkdownConfig, description="Markdown chunking settings"
    )

    @model_validator(mode="after")
    def validate_overlap(self) -> Self:
        """Ensure chunk overlap is less than chunk size."""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"chunk_overlap ({self.chunk_overlap}) must be less than "
                f"chunk_size ({self.chunk_size})"
            )
        return self


class ProcessingConfig(BaseModel):
    """Document processing configuration.

    Attributes:
        parse_pdf_ocr: Enable OCR for PDF parsing.
        parse_images: Extract text from images.
        auto_tag_threshold: Minimum confidence for auto-tagging.
        dedup_strategy: Deduplication strategy (hash or content).
        concurrency: Number of parallel processing workers.
        rate_limit_cloud: Enable rate limiting for cloud APIs.
    """

    model_config = ConfigDict(extra="forbid")

    parse_pdf_ocr: bool = Field(default=True, description="Enable OCR for PDF parsing")
    parse_images: bool = Field(default=True, description="Extract text from images")
    auto_tag_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0, description="Auto-tagging confidence threshold"
    )
    dedup_strategy: DedupStrategy = Field(
        default=DedupStrategy.HASH, description="Deduplication strategy"
    )
    concurrency: int = Field(
        default=4, ge=1, le=32, description="Parallel processing workers"
    )
    rate_limit_cloud: bool = Field(
        default=True, description="Rate limit cloud API calls"
    )


class APIConfig(BaseModel):
    """FastAPI server configuration.

    Attributes:
        host: Bind host address.
        port: Bind port.
        reload: Enable auto-reload (development only).
        workers: Number of worker processes.
        secret_key: Secret key for JWT tokens.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="0.0.0.0", description="Bind host")  # noqa: S104
    port: int = Field(default=8001, ge=1, le=65535, description="Bind port")
    reload: bool = Field(default=False, description="Enable auto-reload (dev only)")
    workers: int = Field(default=4, ge=1, le=64, description="Worker processes")
    secret_key: str = Field(
        default="change-me-in-production",
        description="Secret key for JWT (CHANGE IN PROD!)",
    )


# =============================================================================
# Main Settings Class
# =============================================================================


class YamlConfigSource(PydanticBaseSettingsSource):
    """Custom settings source for loading from YAML config file."""

    def __init__(self, settings_cls: type[BaseSettings], yaml_path: str | None = None):
        self.yaml_path = yaml_path or os.environ.get("GRIMOIRE_CONFIG", "grimoire.yaml")
        super().__init__(settings_cls)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        """Get field value from YAML file."""
        # This is called for each field; we load the whole file once
        return None, "", False

    def __call__(self) -> dict[str, Any]:
        """Load configuration from YAML file."""
        config_path = Path(self.yaml_path)
        if not config_path.exists():
            logger.debug(f"Config file not found: {config_path}")
            return {}

        try:
            with open(config_path, encoding="utf-8") as f:
                config = yaml.safe_load(f)
            # Handle None (empty file) or non-dict content
            if config is None:
                config = {}
            elif not isinstance(config, dict):
                logger.warning(
                    f"YAML config must contain a dict, got {type(config).__name__}"
                )
                return {}
            logger.info(f"Loaded configuration from {config_path}")
            grimoire_config = config.get("grimoire", {})
            # Ensure we return a dict
            return grimoire_config if isinstance(grimoire_config, dict) else {}
        except yaml.YAMLError as e:
            logger.warning(f"Failed to parse YAML config: {e}")
            return {}
        except OSError as e:
            logger.warning(f"Failed to read config file: {e}")
            return {}


class GrimoireSettings(BaseSettings):
    """Main Grimoire configuration class.

    This class manages all configuration settings using Pydantic Settings.
    Settings are loaded from (in order of precedence):
        1. Environment variables (highest priority)
        2. .env file
        3. grimoire.yaml config file
        4. Default values (lowest priority)

    Environment variables use the prefix GRIMOIRE_ and are case-insensitive.
    Example: GRIMOIRE_LLM__MODEL=llama3.2

    Attributes:
        llm: LLM configuration.
        embeddings: Embedding model configuration.
        database: Database configuration.
        vector_store: Vector store configuration.
        logging: Logging configuration.
        celery: Celery task queue configuration.
        redis: Redis configuration.
        query: Query/RAG configuration.
        cache: Cache configuration.
        cloud: Cloud storage configuration.
        watch: File watching configuration.
        observability: Observability configuration.
        chunking: Document chunking configuration.
        processing: Document processing configuration.
        api: API server configuration.
        debug: Debug mode flag.
    """

    model_config = SettingsConfigDict(
        env_prefix="GRIMOIRE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore extra fields for forward compatibility
        validate_default=True,
    )

    # Nested configuration sections
    llm: LLMConfig = Field(default_factory=LLMConfig, description="LLM settings")
    embeddings: EmbeddingsConfig = Field(
        default_factory=EmbeddingsConfig, description="Embedding model settings"
    )
    database: DatabaseConfig = Field(
        default_factory=DatabaseConfig, description="Database settings"
    )
    vector_store: VectorStoreConfig = Field(
        default_factory=VectorStoreConfig, description="Vector store settings"
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig, description="Logging settings"
    )
    celery: CeleryConfig = Field(
        default_factory=CeleryConfig, description="Celery settings"
    )
    redis: RedisConfig = Field(
        default_factory=RedisConfig, description="Redis settings"
    )
    query: QueryConfig = Field(
        default_factory=QueryConfig, description="Query settings"
    )
    cache: CacheConfig = Field(
        default_factory=CacheConfig, description="Cache settings"
    )
    cloud: CloudConfig = Field(
        default_factory=CloudConfig, description="Cloud storage settings"
    )
    watch: WatchConfig = Field(
        default_factory=WatchConfig, description="Watch settings"
    )
    observability: ObservabilityConfig = Field(
        default_factory=ObservabilityConfig, description="Observability settings"
    )
    chunking: ChunkingConfig = Field(
        default_factory=ChunkingConfig, description="Chunking settings"
    )
    processing: ProcessingConfig = Field(
        default_factory=ProcessingConfig, description="Processing settings"
    )
    api: APIConfig = Field(default_factory=APIConfig, description="API settings")

    # Top-level settings
    debug: bool = Field(default=False, description="Debug mode")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Customize settings sources to add YAML config.

        Loading order (highest to lowest priority):
        1. init_settings (values passed to constructor)
        2. env_settings (environment variables)
        3. dotenv_settings (.env file)
        4. file_secret_settings (secret files)
        5. Custom YAML config source
        """
        yaml_source = YamlConfigSource(settings_cls)
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            yaml_source,
        )

    def model_dump_redacted(self) -> dict[str, Any]:
        """Dump settings with sensitive values redacted for logging.

        Returns:
            Dictionary with sensitive fields masked for safe logging.
        """
        data = self.model_dump()

        # List of sensitive field paths to redact
        sensitive_paths = [
            ("database", "url"),
            ("redis", "password"),
            ("cloud", "google", "client_secret"),
            ("cloud", "onedrive", "client_secret"),
            ("api", "secret_key"),
        ]

        for path in sensitive_paths:
            current = data
            for key in path[:-1]:
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    break
            else:
                if isinstance(current, dict) and path[-1] in current:
                    current[path[-1]] = "***REDACTED***"

        return data

    def log_config(self) -> None:
        """Log current configuration at INFO level with sensitive data redacted."""
        config = self.model_dump_redacted()
        logger.info("Grimoire configuration loaded:")
        for section, values in config.items():
            if isinstance(values, dict):
                logger.info(f"  {section}:")
                for key, value in values.items():
                    if isinstance(value, dict):
                        logger.info(f"    {key}: {...}")
                    else:
                        logger.info(f"    {key}: {value}")
            else:
                logger.info(f"  {section}: {values}")


# =============================================================================
# Global Settings Instance
# =============================================================================

# Global settings instance - lazy loaded
_settings: GrimoireSettings | None = None


def get_settings() -> GrimoireSettings:
    """Get or create the global settings instance.

    This function provides access to the global settings singleton.
    Settings are loaded once and cached for subsequent calls.

    Returns:
        GrimoireSettings: The global settings instance.

    Example:
        >>> from grimoire.config import get_settings
        >>> settings = get_settings()
        >>> print(settings.llm.model)
    """
    global _settings
    if _settings is None:
        try:
            _settings = GrimoireSettings()
            if _settings.debug:
                _settings.log_config()
        except ValidationError as e:
            logger.error("Failed to load configuration:")
            for error in e.errors():
                logger.error(f"  {error['loc']}: {error['msg']}")
            raise
    return _settings


def reload_settings() -> GrimoireSettings:
    """Reload settings from all sources.

    This clears the cached settings and reloads from environment
    variables, .env file, and YAML config.

    Returns:
        GrimoireSettings: The reloaded settings instance.

    Example:
        >>> from grimoire.config import reload_settings
        >>> settings = reload_settings()
    """
    global _settings
    _settings = None
    return get_settings()


# Convenience import
settings = get_settings()
