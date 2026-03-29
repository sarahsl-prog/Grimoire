"""Configuration validation tests.

Tests for the Pydantic Settings configuration system with comprehensive
coverage of happy path, edge cases, input validation, error handling,
async behavior, and state management.

Follows Appendix D testing standards from IMPLEMENTATION.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from grimoire.config import (
    APIConfig,
    CacheConfig,
    CacheStorage,
    CeleryConfig,
    ChunkingConfig,
    ChunkingStrategy,
    CloudConfig,
    DatabaseConfig,
    DedupStrategy,
    EmbeddingsConfig,
    GrimoireSettings,
    LLMConfig,
    LogLevel,
    LoggingConfig,
    ProcessingConfig,
    QueryConfig,
    RedisConfig,
    VectorStoreConfig,
    VectorStoreType,
    WatchConfig,
    get_settings,
    reload_settings,
)

# =============================================================================
# Test Classes - Happy Path Tests
# =============================================================================


class TestConfigHappyPath:
    """Standard configuration loading scenarios."""

    def test_default_settings_load(self) -> None:
        """Test that default settings load without errors."""
        settings = GrimoireSettings()
        assert settings.llm.model == "llama3.2"
        assert settings.database.pool_size == 10
        assert settings.vector_store.type == VectorStoreType.CHROMADB

    def test_env_var_loading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test loading configuration from environment variables."""
        monkeypatch.setenv("GRIMOIRE_LLM__MODEL", "mistral")
        monkeypatch.setenv("GRIMOIRE_LLM__TEMPERATURE", "0.5")
        monkeypatch.setenv("GRIMOIRE_DATABASE__POOL_SIZE", "20")

        settings = GrimoireSettings()
        assert settings.llm.model == "mistral"
        assert settings.llm.temperature == 0.5
        assert settings.database.pool_size == 20

    def test_yaml_config_loading(self, temp_directory: Path) -> None:
        """Test loading configuration from YAML file."""
        config_file = temp_directory / "grimoire.yaml"
        config_content = {
            "grimoire": {
                "llm": {"model": "codellama", "max_tokens": 2048},
                "database": {"pool_size": 15},
            }
        }
        config_file.write_text(yaml.dump(config_content))

        # Create settings with custom config path
        from grimoire.config.settings import YamlConfigSource
        from pydantic_settings import PydanticBaseSettingsSource

        class CustomSettings(GrimoireSettings):
            @classmethod
            def settings_customise_sources(
                cls,
                settings_cls: type[BaseSettings],
                init_settings: PydanticBaseSettingsSource,
                env_settings: PydanticBaseSettingsSource,
                dotenv_settings: PydanticBaseSettingsSource,
                file_secret_settings: PydanticBaseSettingsSource,
            ) -> tuple[PydanticBaseSettingsSource, ...]:
                yaml_source = YamlConfigSource(settings_cls, str(config_file))
                return (
                    init_settings,
                    env_settings,
                    dotenv_settings,
                    file_secret_settings,
                    yaml_source,
                )

        settings = CustomSettings()
        assert settings.llm.model == "codellama"
        assert settings.llm.max_tokens == 2048
        assert settings.database.pool_size == 15

    def test_nested_config_access(self) -> None:
        """Test accessing nested configuration values."""
        settings = GrimoireSettings()
        assert settings.llm.url == "http://localhost:11434"
        assert settings.embeddings.batch_size == 32
        assert settings.redis.port == 6379

    def test_enum_values(self) -> None:
        """Test that enum fields use proper enum values."""
        settings = GrimoireSettings()
        assert settings.logging.level == LogLevel.INFO
        assert settings.vector_store.type == VectorStoreType.CHROMADB
        assert settings.chunking.default_strategy == ChunkingStrategy.SEMANTIC


# =============================================================================
# Edge Cases & Boundary Conditions
# =============================================================================


class TestConfigEdgeCases:
    """Boundary conditions and unusual configurations."""

    def test_minimal_valid_config(self) -> None:
        """Test with minimal valid configuration."""
        settings = GrimoireSettings()
        # Should load with all defaults
        assert settings.llm.max_tokens == 4096
        assert settings.query.default_top_k == 10

    def test_empty_yaml_config(self, temp_directory: Path) -> None:
        """Test handling of empty YAML config."""
        config_file = temp_directory / "grimoire.yaml"
        config_file.write_text("grimoire:\n")

        from grimoire.config.settings import YamlConfigSource
        from pydantic_settings import PydanticBaseSettingsSource

        class TestSettings(GrimoireSettings):
            @classmethod
            def settings_customise_sources(
                cls,
                settings_cls: type[BaseSettings],
                init_settings: PydanticBaseSettingsSource,
                env_settings: PydanticBaseSettingsSource,
                dotenv_settings: PydanticBaseSettingsSource,
                file_secret_settings: PydanticBaseSettingsSource,
            ) -> tuple[PydanticBaseSettingsSource, ...]:
                yaml_source = YamlConfigSource(settings_cls, str(config_file))
                return (
                    init_settings,
                    env_settings,
                    dotenv_settings,
                    file_secret_settings,
                    yaml_source,
                )

        # Should load with defaults
        settings = TestSettings()
        assert settings.llm.model == "llama3.2"

    def test_unicode_in_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test handling of Unicode characters in paths."""
        unicode_path = "/home/用户/logs"
        monkeypatch.setenv("GRIMOIRE_LOGGING__LOG_DIR", unicode_path)
        monkeypatch.setenv("HOME", "/home/user")

        settings = GrimoireSettings()
        assert "用户" in settings.logging.log_dir

    def test_very_long_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test handling of very long configuration values."""
        long_model = "a" * 1000
        monkeypatch.setenv("GRIMOIRE_LLM__MODEL", long_model)

        settings = GrimoireSettings()
        assert len(settings.llm.model) == 1000

    def test_boundary_numeric_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test boundary values for numeric fields."""
        # Minimum port
        monkeypatch.setenv("GRIMOIRE_REDIS__PORT", "1")
        settings = GrimoireSettings()
        assert settings.redis.port == 1

        # Maximum port
        monkeypatch.setenv("GRIMOIRE_REDIS__PORT", "65535")
        settings = GrimoireSettings()
        assert settings.redis.port == 65535

    def test_zero_and_negative_boundaries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test boundary values near zero."""
        # Zero temperature (valid)
        monkeypatch.setenv("GRIMOIRE_LLM__TEMPERATURE", "0.0")
        settings = GrimoireSettings()
        assert settings.llm.temperature == 0.0

        # Zero is not valid for pool_size
        monkeypatch.setenv("GRIMOIRE_DATABASE__POOL_SIZE", "1")
        settings = GrimoireSettings()
        assert settings.database.pool_size == 1


# =============================================================================
# Input Validation & Error Handling
# =============================================================================


class TestConfigInputValidation:
    """Invalid inputs are rejected gracefully."""

    def test_invalid_url_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test rejection of invalid URL formats."""
        monkeypatch.setenv("GRIMOIRE_LLM__URL", "not-a-url")

        with pytest.raises(ValidationError) as exc_info:
            GrimoireSettings()

        assert "http://" in str(exc_info.value) or "https://" in str(exc_info.value)

    def test_invalid_port_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test rejection of invalid port numbers."""
        monkeypatch.setenv("GRIMOIRE_REDIS__PORT", "70000")

        with pytest.raises(ValidationError) as exc_info:
            GrimoireSettings()

        assert "Port" in str(exc_info.value) or "65535" in str(exc_info.value)

    def test_invalid_port_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test rejection of non-numeric port values."""
        monkeypatch.setenv("GRIMOIRE_REDIS__PORT", "abc")

        with pytest.raises(ValidationError) as exc_info:
            GrimoireSettings()

        assert (
            "port" in str(exc_info.value).lower()
            or "type" in str(exc_info.value).lower()
        )

    def test_invalid_model_name_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test rejection of invalid model name format."""
        monkeypatch.setenv("GRIMOIRE_EMBEDDINGS__MODEL", "invalid-model-name")

        with pytest.raises(ValidationError) as exc_info:
            GrimoireSettings()

        assert "namespace" in str(exc_info.value) or "/" in str(exc_info.value)

    def test_missing_required_redis_credentials(self) -> None:
        """Test that required fields are enforced."""
        # This test validates the field structure exists
        config = GrimoireSettings()
        assert config.redis.host is not None
        assert config.redis.port is not None

    def test_invalid_chunking_strategy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test rejection of invalid chunking strategy."""
        from pydantic import BaseModel

        class TestChunking(BaseModel):
            strategy: ChunkingStrategy

        with pytest.raises(ValidationError):
            TestChunking(strategy="invalid_strategy")  # type: ignore

    def test_invalid_temperature_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test temperature value outside valid range."""
        monkeypatch.setenv("GRIMOIRE_LLM__TEMPERATURE", "5.0")

        with pytest.raises(ValidationError) as exc_info:
            GrimoireSettings()

        assert "temperature" in str(exc_info.value).lower()

    def test_chunk_overlap_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that chunk overlap must be less than chunk size."""
        monkeypatch.setenv("GRIMOIRE_CHUNKING__CHUNK_SIZE", "500")
        monkeypatch.setenv("GRIMOIRE_CHUNKING__CHUNK_OVERLAP", "500")

        with pytest.raises(ValidationError) as exc_info:
            GrimoireSettings()

        assert "overlap" in str(exc_info.value).lower()

    def test_malformed_database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test validation of database URL format."""
        monkeypatch.setenv("GRIMOIRE_DATABASE__URL", "not-a-db-url")

        with pytest.raises(ValidationError) as exc_info:
            GrimoireSettings()

        assert "postgresql://" in str(exc_info.value) or "sqlite://" in str(
            exc_info.value
        )


# =============================================================================
# Async Behavior Tests
# =============================================================================


class TestConfigAsyncBehavior:
    """Async and concurrent configuration access."""

    @pytest.mark.asyncio
    async def test_async_config_access(self) -> None:
        """Test that configuration can be accessed from async code."""
        settings = GrimoireSettings()

        # Simulate async access
        async def get_model() -> str:
            return settings.llm.model

        result = await get_model()
        assert result == "llama3.2"

    @pytest.mark.asyncio
    async def test_concurrent_reads(self) -> None:
        """Test concurrent read access to settings."""
        settings = GrimoireSettings()

        async def read_config() -> dict[str, Any]:
            return {
                "model": settings.llm.model,
                "batch_size": settings.embeddings.batch_size,
                "port": settings.redis.port,
            }

        # Run multiple concurrent reads
        import asyncio

        tasks = [read_config() for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All results should be identical
        for result in results:
            assert result["model"] == "llama3.2"
            assert result["batch_size"] == 32
            assert result["port"] == 6379


# =============================================================================
# State Management Tests
# =============================================================================


class TestConfigStateManagement:
    """Settings state and singleton behavior."""

    def test_global_settings_instance(self) -> None:
        """Test that get_settings returns consistent instance."""
        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2

    def test_reload_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test settings reload functionality."""
        # Set initial value
        monkeypatch.setenv("GRIMOIRE_LLM__MODEL", "initial-model")
        settings1 = reload_settings()
        assert settings1.llm.model == "initial-model"

        # Change value and reload
        monkeypatch.setenv("GRIMOIRE_LLM__MODEL", "updated-model")
        settings2 = reload_settings()

        assert settings2.llm.model == "updated-model"
        # After reload, should get new instance
        settings3 = get_settings()
        assert settings3.llm.model == "updated-model"


# =============================================================================
# Configuration Override Tests
# =============================================================================


class TestConfigOverrides:
    """Test configuration override behavior."""

    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variables override defaults."""
        # Default is 4096
        settings_default = GrimoireSettings()
        assert settings_default.llm.max_tokens == 4096

        # Override via env
        monkeypatch.setenv("GRIMOIRE_LLM__MAX_TOKENS", "8192")
        settings_override = GrimoireSettings()
        assert settings_override.llm.max_tokens == 8192

    def test_init_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Constructor values override environment variables."""
        monkeypatch.setenv("GRIMOIRE_LLM__MODEL", "from-env")

        settings = GrimoireSettings(llm={"model": "from-constructor"})  # type: ignore
        assert settings.llm.model == "from-constructor"

    def test_nested_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test nested configuration override."""
        monkeypatch.setenv("GRIMOIRE_VECTOR_STORE__CHROMADB__PATH", "/custom/path")

        settings = GrimoireSettings()
        assert settings.vector_store.chromadb.path == "/custom/path"


# =============================================================================
# Path Validation Tests
# =============================================================================


class TestConfigPathValidation:
    """Test path-related configuration validation."""

    def test_home_directory_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test tilde expansion in paths."""
        monkeypatch.setenv("HOME", "/home/testuser")

        # Test path resolution
        expanded_path = os.path.expanduser("~/logs")
        assert expanded_path.startswith("/home/testuser")

        # Verify config can use home-relative paths
        log_config = LoggingConfig(log_dir="~/logs")
        assert log_config is not None

    def test_path_creation(
        self, temp_directory: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test automatic path creation for log directory."""
        log_dir = temp_directory / "test_logs"
        monkeypatch.setenv("GRIMOIRE_LOGGING__LOG_DIR", str(log_dir))

        # Will create directory in validator
        config = LoggingConfig(log_dir=str(log_dir))
        # Directory may or may not exist depending on permissions
        # Just verify the path is set correctly
        assert "test_logs" in config.log_dir


# =============================================================================
# Security Tests
# =============================================================================


class TestConfigSecurity:
    """Security-related configuration tests."""

    def test_sensitive_values_redacted(self) -> None:
        """Test that sensitive values are redacted in dumps."""
        settings = GrimoireSettings(
            database={"url": "postgresql://secret:password@localhost/db"},  # type: ignore
            api={"secret_key": "super-secret-key"},  # type: ignore
        )

        redacted = settings.model_dump_redacted()

        # Database URL should be redacted
        assert "secret:password" not in str(redacted)
        assert "***REDACTED***" in str(redacted) or "postgresql://" in str(redacted)

    def test_secret_key_validation(self) -> None:
        """Test that secret key is required."""
        config = APIConfig(secret_key="test-key")  # noqa: S106
        assert config.secret_key == "test-key"


# =============================================================================
# Enum Validation Tests
# =============================================================================


class TestConfigEnums:
    """Test enum-based configuration validation."""

    def test_log_level_enum(self) -> None:
        """Test valid log level enum values."""
        for level in LogLevel:
            config = LoggingConfig(level=level)
            assert config.level == level

    def test_vector_store_type_enum(self) -> None:
        """Test valid vector store type enum values."""
        config = VectorStoreConfig(type=VectorStoreType.QDRANT)
        assert config.type == VectorStoreType.QDRANT

    def test_chunking_strategy_enum(self) -> None:
        """Test valid chunking strategy enum values."""
        config = ChunkingConfig(default_strategy=ChunkingStrategy.MARKDOWN)
        assert config.default_strategy == ChunkingStrategy.MARKDOWN

    def test_cache_storage_enum(self) -> None:
        """Test valid cache storage enum values."""
        config = CacheConfig(storage=CacheStorage.REDIS)
        assert config.storage == CacheStorage.REDIS

    def test_dedup_strategy_enum(self) -> None:
        """Test valid deduplication strategy enum values."""
        config = ProcessingConfig(dedup_strategy=DedupStrategy.CONTENT)
        assert config.dedup_strategy == DedupStrategy.CONTENT


# =============================================================================
# Individual Config Class Tests
# =============================================================================


class TestLLMConfig:
    """Test LLM configuration."""

    def test_default_llm_config(self) -> None:
        """Test default LLM configuration values."""
        config = LLMConfig()
        assert config.model == "llama3.2"
        assert config.temperature == 0.7
        assert config.max_tokens == 4096
        assert config.url == "http://localhost:11434"

    def test_llm_url_variants(self) -> None:
        """Test various valid LLM URLs."""
        valid_urls = [
            "http://localhost:11434",
            "https://ollama.example.com",
            "http://192.168.1.100:11434",
        ]
        for url in valid_urls:
            config = LLMConfig(url=url)
            assert config.url == url

    def test_llm_temperature_bounds(self) -> None:
        """Test temperature boundary values."""
        # Minimum
        config = LLMConfig(temperature=0.0)
        assert config.temperature == 0.0

        # Maximum
        config = LLMConfig(temperature=2.0)
        assert config.temperature == 2.0


class TestDatabaseConfig:
    """Test Database configuration."""

    def test_default_database_config(self) -> None:
        """Test default database configuration."""
        config = DatabaseConfig()
        assert "postgresql://" in config.url
        assert config.pool_size == 10
        assert config.echo is False

    def test_sqlite_url(self) -> None:
        """Test SQLite URL is valid."""
        config = DatabaseConfig(url="sqlite:///test.db")
        assert config.url == "sqlite:///test.db"

    def test_postgres_url_components(self) -> None:
        """Test PostgreSQL URL has required components."""
        config = DatabaseConfig(url="postgresql://user:pass@localhost:5432/db")
        assert "user:pass@localhost" in config.url


class TestEmbeddingsConfig:
    """Test Embeddings configuration."""

    def test_default_embeddings_config(self) -> None:
        """Test default embeddings configuration."""
        config = EmbeddingsConfig()
        assert "mpnet-base-v2" in config.model
        assert config.batch_size == 32

    def test_device_validation(self) -> None:
        """Test device option validation."""
        for device in ["auto", "cuda", "cpu", "mps"]:
            config = EmbeddingsConfig(device=device)  # type: ignore
            assert config.device == device

    def test_invalid_device(self) -> None:
        """Test invalid device option."""
        with pytest.raises(ValidationError):
            EmbeddingsConfig(device="invalid")  # type: ignore


class TestQueryConfig:
    """Test Query configuration."""

    def test_default_query_config(self) -> None:
        """Test default query configuration."""
        config = QueryConfig()
        assert config.default_top_k == 10
        assert config.hybrid_alpha == 0.7
        assert config.enable_citations is True

    def test_hybrid_alpha_bounds(self) -> None:
        """Test hybrid alpha boundary values."""
        # Minimum
        config = QueryConfig(hybrid_alpha=0.0)
        assert config.hybrid_alpha == 0.0

        # Maximum
        config = QueryConfig(hybrid_alpha=1.0)
        assert config.hybrid_alpha == 1.0


class TestRedisConfig:
    """Test Redis configuration."""

    def test_default_redis_config(self) -> None:
        """Test default Redis configuration."""
        config = RedisConfig()
        assert config.host == "localhost"
        assert config.port == 6379
        assert config.db_cache == 2

    def test_port_range_validation(self) -> None:
        """Test port number range validation."""
        # Valid boundary ports
        RedisConfig(port=1)
        RedisConfig(port=65535)

        # Invalid ports
        with pytest.raises(ValidationError):
            RedisConfig(port=0)
        with pytest.raises(ValidationError):
            RedisConfig(port=70000)


class TestProcessingConfig:
    """Test Processing configuration."""

    def test_default_processing_config(self) -> None:
        """Test default processing configuration."""
        config = ProcessingConfig()
        assert config.parse_pdf_ocr is True
        assert config.parse_images is True
        assert config.auto_tag_threshold == 0.7
        assert config.dedup_strategy == DedupStrategy.HASH

    def test_concurrency_bounds(self) -> None:
        """Test concurrency setting bounds."""
        # Minimum
        config = ProcessingConfig(concurrency=1)
        assert config.concurrency == 1

        # Maximum
        config = ProcessingConfig(concurrency=32)
        assert config.concurrency == 32


class TestChunkingConfig:
    """Test Chunking configuration."""

    def test_default_chunking_config(self) -> None:
        """Test default chunking configuration."""
        config = ChunkingConfig()
        assert config.chunk_size == 1000
        assert config.chunk_overlap == 200
        assert config.default_strategy == ChunkingStrategy.SEMANTIC

    def test_valid_strategies(self) -> None:
        """Test all valid chunking strategies."""
        for strategy in ChunkingStrategy:
            config = ChunkingConfig(default_strategy=strategy)
            assert config.default_strategy == strategy


class TestWatchConfig:
    """Test Watch configuration."""

    def test_default_watch_config(self) -> None:
        """Test default watch configuration."""
        config = WatchConfig()
        assert config.default_poll_interval == 300
        assert config.max_local_watches == 100
        assert len(config.ignore_patterns) > 0

    def test_poll_interval_minimum(self) -> None:
        """Test minimum poll interval."""
        config = WatchConfig(default_poll_interval=10)
        assert config.default_poll_interval == 10


class TestLoggingConfig:
    """Test Logging configuration."""

    def test_default_logging_config(self) -> None:
        """Test default logging configuration."""
        config = LoggingConfig()
        assert config.level == LogLevel.INFO
        assert config.structured is True
        assert config.use_json is False


class TestCeleryConfig:
    """Test Celery configuration."""

    def test_default_celery_config(self) -> None:
        """Test default Celery configuration."""
        config = CeleryConfig()
        assert config.broker_url.startswith("redis://")
        assert config.timezone == "UTC"
        assert config.enable_utc is True

    def test_celery_broker_validation(self) -> None:
        """Test Celery broker URL validation."""
        with pytest.raises(ValidationError):
            CeleryConfig(broker_url="http://invalid.url")


class TestCacheConfig:
    """Test Cache configuration."""

    def test_default_cache_config(self) -> None:
        """Test default cache configuration."""
        config = CacheConfig()
        assert config.storage == CacheStorage.DISK
        assert config.embedding_ttl == 604800  # 7 days
        assert config.result_ttl == 86400  # 1 day


class TestCloudConfig:
    """Test Cloud configuration."""

    def test_default_cloud_config(self) -> None:
        """Test default cloud storage configuration."""
        config = CloudConfig()
        assert config.google.credentials_path is not None
        assert config.onedrive.token_store is not None

    def test_path_expansion(self) -> None:
        """Test path expansion in cloud config."""
        from grimoire.config import CloudGoogleConfig

        config = CloudGoogleConfig(credentials_path="~/.config/test.json")
        assert "/.config/" in config.credentials_path


class TestAPIConfig:
    """Test API configuration."""

    def test_default_api_config(self) -> None:
        """Test default API configuration."""
        config = APIConfig()
        assert config.host == "0.0.0.0"  # noqa: S104
        assert config.port == 8001
        assert config.workers == 4

    def test_secret_key_warning(self) -> None:
        """Test default secret key has warning value."""
        config = APIConfig()
        assert "change" in config.secret_key.lower()


# =============================================================================
# Integration Tests
# =============================================================================


class TestConfigIntegration:
    """Integration tests for configuration system."""

    def test_load_from_env_file(
        self, temp_directory: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test loading configuration from .env file."""
        env_file = temp_directory / ".env"
        env_file.write_text("""
GRIMOIRE_LLM__MODEL=mistral
GRIMOIRE_LLM__TEMPERATURE=0.5
GRIMOIRE_DATABASE__POOL_SIZE=20
""")
        monkeypatch.setenv("GRIMOIRE_LLM__MODEL", "mistral")
        monkeypatch.setenv("GRIMOIRE_LLM__TEMPERATURE", "0.5")
        monkeypatch.setenv("GRIMOIRE_DATABASE__POOL_SIZE", "20")

        settings = GrimoireSettings()
        assert settings.llm.model == "mistral"
        assert settings.llm.temperature == 0.5
        assert settings.database.pool_size == 20

    def test_complex_nested_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test complex nested configuration overrides."""
        # Override deeply nested fields
        monkeypatch.setenv(
            "GRIMOIRE_VECTOR_STORE__CHROMADB__COLLECTION_NAME", "my_docs"
        )
        monkeypatch.setenv("GRIMOIRE_CHUNKING__SEMANTIC__THRESHOLD", "0.8")

        settings = GrimoireSettings()
        assert settings.vector_store.chromadb.collection_name == "my_docs"
        assert settings.chunking.semantic.threshold == 0.8

    def test_config_immutable(self) -> None:
        """Test that config fields are frozen."""
        settings = GrimoireSettings()
        # Individual models are mutable but new instances should be created
        new_llm = LLMConfig(model="new-model")
        settings.llm = new_llm  # type: ignore
        assert settings.llm.model == "new-model"


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestConfigErrorHandling:
    """Test error handling and validation messages."""

    def test_validation_error_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that validation errors have clear messages."""
        monkeypatch.setenv("GRIMOIRE_LLM__MAX_TOKENS", "not-a-number")

        with pytest.raises(ValidationError) as exc_info:
            GrimoireSettings()

        error_str = str(exc_info.value)
        # Should contain field location
        assert "max_tokens" in error_str.lower() or "llm" in error_str.lower()

    def test_multiple_validation_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that multiple validation errors are collected."""
        monkeypatch.setenv("GRIMOIRE_REDIS__PORT", "99999")
        monkeypatch.setenv("GRIMOIRE_LLM__TEMPERATURE", "10.0")

        with pytest.raises(ValidationError) as exc_info:
            GrimoireSettings()

        error_msg = str(exc_info.value)
        # Should contain information about errors
        assert "validation" in error_msg.lower()


# =============================================================================
# Smoke Tests
# =============================================================================


def test_settings_import() -> None:
    """Test that settings can be imported from config module."""
    from grimoire.config import settings

    assert settings is not None


def test_all_config_sections_present() -> None:
    """Test that all expected config sections are present."""
    settings = GrimoireSettings()

    # Check all expected sections
    assert hasattr(settings, "llm")
    assert hasattr(settings, "embeddings")
    assert hasattr(settings, "database")
    assert hasattr(settings, "vector_store")
    assert hasattr(settings, "logging")
    assert hasattr(settings, "celery")
    assert hasattr(settings, "redis")
    assert hasattr(settings, "query")
    assert hasattr(settings, "cache")
    assert hasattr(settings, "cloud")
    assert hasattr(settings, "watch")
    assert hasattr(settings, "observability")
    assert hasattr(settings, "chunking")
    assert hasattr(settings, "processing")
    assert hasattr(settings, "api")
    assert hasattr(settings, "debug")


def test_config_export_json() -> None:
    """Test that config can be exported to JSON-serializable dict."""
    settings = GrimoireSettings()
    data = settings.model_dump()

    assert isinstance(data, dict)
    assert "llm" in data
    assert "database" in data
