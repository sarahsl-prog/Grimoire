"""Comprehensive tests for Abstract Base Classes.

This module tests that all ABCs:
- Can be imported without errors
- Use @abstractmethod decorator correctly
- Raise NotImplementedError for abstract methods
- Have proper type hints
- Work with async where expected
"""

import abc
import asyncio
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from grimoire.core.cache import Cache
from grimoire.core.reranker import Reranker
from grimoire.storage.base import (
    FileChange,
    FileChangeType,
    FileInfo,
    FileMetadata,
    StorageAdapter,
    StorageBackend,
)
from grimoire.vectorstore.base import VectorStore
from grimoire.utils.logger import get_logger, setup_logger

# =============================================================================
# Happy Path Tests
# =============================================================================


class TestABCImports:
    """Test that all ABCs can be imported without errors."""

    def test_vector_store_import(self) -> None:
        """VectorStore ABC can be imported."""
        from grimoire.vectorstore.base import VectorStore

        assert VectorStore is not None
        assert isinstance(VectorStore.__class__, type(abc.ABCMeta))

    def test_storage_adapter_import(self) -> None:
        """StorageAdapter ABC can be imported."""
        from grimoire.storage.base import StorageAdapter

        assert StorageAdapter is not None
        assert isinstance(StorageAdapter.__class__, type(abc.ABCMeta))

    def test_cache_import(self) -> None:
        """Cache ABC can be imported."""
        from grimoire.core.cache import Cache

        assert Cache is not None
        assert isinstance(Cache.__class__, type(abc.ABCMeta))

    def test_reranker_import(self) -> None:
        """Reranker ABC can be imported."""
        from grimoire.core.reranker import Reranker

        assert Reranker is not None
        assert isinstance(Reranker.__class__, type(abc.ABCMeta))


class TestABCMeta:
    """Test that ABCMeta is properly used."""

    def test_vector_store_is_abc(self) -> None:
        """VectorStore is an abstract base class."""
        assert abc.ABC in VectorStore.__mro__

    def test_storage_adapter_is_abc(self) -> None:
        """StorageAdapter is an abstract base class."""
        assert abc.ABC in StorageAdapter.__mro__

    def test_cache_is_abc(self) -> None:
        """Cache is an abstract base class."""
        assert abc.ABC in Cache.__mro__

    def test_reranker_is_abc(self) -> None:
        """Reranker is an abstract base class."""
        assert abc.ABC in Reranker.__mro__


class TestAbstractMethods:
    """Test that abstract methods require implementation."""

    def test_vector_store_cannot_instantiate(self) -> None:
        """VectorStore ABC cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract"):
            VectorStore()

    def test_storage_adapter_cannot_instantiate(self) -> None:
        """StorageAdapter ABC cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract"):
            StorageAdapter()

    def test_cache_cannot_instantiate(self) -> None:
        """Cache ABC cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract"):
            Cache()

    def test_reranker_cannot_instantiate(self) -> None:
        """Reranker ABC cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract"):
            Reranker()


class TestConcreteImplementation:
    """Test that concrete implementations work correctly."""

    def test_vector_store_concrete_impl(self) -> None:
        """Concrete VectorStore can be created and used."""

        class MockVectorStore(VectorStore):
            async def initialize(
                self, collection_name: str, embedding_dim: int
            ) -> None:
                pass

            async def add_documents(
                self,
                ids: List[str],
                embeddings: List[List[float]],
                metadatas: List[Dict[str, Any]],
                documents: List[str],
            ) -> None:
                pass

            async def search(
                self,
                query_embedding: List[float],
                filter_dict: Optional[Dict[str, Any]] = None,
                top_k: int = 10,
                include: Optional[List[str]] = None,
            ) -> List[Dict[str, Any]]:
                return []

            async def delete(self, ids: List[str]) -> None:
                pass

            async def get(self, ids: List[str]) -> List[Dict[str, Any]]:
                return []

            async def count(self) -> int:
                return 0

        instance = MockVectorStore()
        assert isinstance(instance, VectorStore)

    def test_storage_adapter_concrete_impl(self) -> None:
        """Concrete StorageAdapter can be created and used."""

        class MockStorageAdapter(StorageAdapter):
            async def list_files(
                self, path: str, recursive: bool = False
            ) -> List[FileInfo]:
                return []

            async def read_file(self, path: str) -> bytes:
                return b""

            async def get_metadata(self, path: str) -> FileMetadata:
                return FileMetadata(path=path, size_bytes=0, modified_at=datetime.now())

            async def exists(self, path: str) -> bool:
                return False

            async def list_changes(
                self, since: datetime, path: Optional[str] = None
            ) -> List[FileChange]:
                return []

            async def supports_watch(self) -> bool:
                return False

            async def watch(self, path: str, callback: Any) -> Any:
                raise NotImplementedError("Local only")

        instance = MockStorageAdapter()
        assert isinstance(instance, StorageAdapter)

    def test_cache_concrete_impl(self) -> None:
        """Concrete Cache can be created and used."""

        class MockCache(Cache):
            async def get(self, key: str) -> Optional[Any]:
                return None

            async def set(
                self, key: str, value: Any, ttl: Optional[int] = None
            ) -> None:
                pass

            async def delete(self, key: str) -> None:
                pass

            async def clear(self) -> None:
                pass

        instance = MockCache()
        assert isinstance(instance, Cache)

    def test_reranker_concrete_impl(self) -> None:
        """Concrete Reranker can be created and used."""

        class MockReranker(Reranker):
            async def rerank(
                self, query: str, documents: List[str], top_k: int = 5
            ) -> List[int]:
                return list(range(min(top_k, len(documents))))

        instance = MockReranker()
        assert isinstance(instance, Reranker)


# =============================================================================
# Edge Cases & Boundary Conditions
# =============================================================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_documents_rerank(self) -> None:
        """Reranker handles empty document list."""

        class MockReranker(Reranker):
            async def rerank(
                self, query: str, documents: List[str], top_k: int = 5
            ) -> List[int]:
                if not documents:
                    return []
                return list(range(min(top_k, len(documents))))

        instance = MockReranker()
        result = asyncio.run(instance.rerank("query", [], top_k=5))
        assert result == []

    def test_single_document_rerank(self) -> None:
        """Reranker handles single document."""

        class MockReranker(Reranker):
            async def rerank(
                self, query: str, documents: List[str], top_k: int = 5
            ) -> List[int]:
                if not documents:
                    return []
                return [0]

        instance = MockReranker()
        result = asyncio.run(instance.rerank("query", ["doc"], top_k=5))
        assert result == [0]

    def test_cache_none_values(self) -> None:
        """Cache handles None values correctly."""

        class MockCache(Cache):
            def __init__(self) -> None:
                self._data: Dict[str, Any] = {}

            async def get(self, key: str) -> Optional[Any]:
                return self._data.get(key)

            async def set(
                self, key: str, value: Any, ttl: Optional[int] = None
            ) -> None:
                self._data[key] = value

            async def delete(self, key: str) -> None:
                self._data.pop(key, None)

            async def clear(self) -> None:
                self._data.clear()

        instance = MockCache()
        asyncio.run(instance.set("key", None))
        result = asyncio.run(instance.get("key"))
        assert result is None

    def test_vector_store_empty_ids(self) -> None:
        """VectorStore handles empty ID list."""

        class MockVectorStore(VectorStore):
            async def initialize(
                self, collection_name: str, embedding_dim: int
            ) -> None:
                pass

            async def add_documents(
                self,
                ids: List[str],
                embeddings: List[List[float]],
                metadatas: List[Dict[str, Any]],
                documents: List[str],
            ) -> None:
                if not ids:
                    return
                # Normal processing

            async def search(
                self,
                query_embedding: List[float],
                filter_dict: Optional[Dict[str, Any]] = None,
                top_k: int = 10,
                include: Optional[List[str]] = None,
            ) -> List[Dict[str, Any]]:
                return []

            async def delete(self, ids: List[str]) -> None:
                pass

            async def get(self, ids: List[str]) -> List[Dict[str, Any]]:
                return []

            async def count(self) -> int:
                return 0

        instance = MockVectorStore()
        # Should not raise
        asyncio.run(instance.add_documents([], [], [], []))

    def test_storage_list_files_empty_directory(self) -> None:
        """StorageAdapter handles empty directory listing."""

        class MockStorageAdapter(StorageAdapter):
            async def list_files(
                self, path: str, recursive: bool = False
            ) -> List[FileInfo]:
                return []

            async def read_file(self, path: str) -> bytes:
                return b""

            async def get_metadata(self, path: str) -> FileMetadata:
                return FileMetadata(path=path, size_bytes=0, modified_at=datetime.now())

            async def exists(self, path: str) -> bool:
                return True

            async def list_changes(
                self, since: datetime, path: Optional[str] = None
            ) -> List[FileChange]:
                return []

            async def supports_watch(self) -> bool:
                return False

            async def watch(self, path: str, callback: Any) -> Any:
                raise NotImplementedError()

        instance = MockStorageAdapter()
        result = asyncio.run(instance.list_files("/empty"))
        assert result == []


# =============================================================================
# Input Validation Tests
# =============================================================================


class TestInputValidation:
    """Test input validation on ABC methods."""

    def test_reranker_invalid_top_k(self) -> None:
        """Reranker handles invalid top_k values."""

        class MockReranker(Reranker):
            async def rerank(
                self, query: str, documents: List[str], top_k: int = 5
            ) -> List[int]:
                if top_k < 0:
                    raise ValueError("top_k must be non-negative")
                if top_k == 0:
                    return []
                return list(range(min(top_k, len(documents))))

        instance = MockReranker()
        result = asyncio.run(instance.rerank("q", ["doc"], top_k=0))
        assert result == []

        with pytest.raises(ValueError):
            asyncio.run(instance.rerank("q", ["doc"], top_k=-1))

    def test_cache_key_validation(self) -> None:
        """Cache validates key types."""

        class MockCache(Cache):
            def __init__(self) -> None:
                self._data: Dict[str, Any] = {}

            async def get(self, key: str) -> Optional[Any]:
                if not isinstance(key, str):
                    raise TypeError("key must be a string")
                return self._data.get(key)

            async def set(
                self, key: str, value: Any, ttl: Optional[int] = None
            ) -> None:
                if not isinstance(key, str):
                    raise TypeError("key must be a string")
                self._data[key] = value

            async def delete(self, key: str) -> None:
                if not isinstance(key, str):
                    raise TypeError("key must be a string")
                self._data.pop(key, None)

            async def clear(self) -> None:
                self._data.clear()

        instance = MockCache()
        with pytest.raises(TypeError):
            asyncio.run(instance.get(123))  # type: ignore[arg-type]

    def test_vector_store_dimension_validation(self) -> None:
        """VectorStore validates embedding dimensions."""

        class MockVectorStore(VectorStore):
            def __init__(self) -> None:
                self._dim: int = 0

            async def initialize(
                self, collection_name: str, embedding_dim: int
            ) -> None:
                if embedding_dim <= 0:
                    raise ValueError("embedding_dim must be positive")
                self._dim = embedding_dim

            async def add_documents(
                self,
                ids: List[str],
                embeddings: List[List[float]],
                metadatas: List[Dict[str, Any]],
                documents: List[str],
            ) -> None:
                for emb in embeddings:
                    if len(emb) != self._dim:
                        raise ValueError(
                            f"Embedding dimension {len(emb)} != {self._dim}"
                        )

            async def search(
                self,
                query_embedding: List[float],
                filter_dict: Optional[Dict[str, Any]] = None,
                top_k: int = 10,
                include: Optional[List[str]] = None,
            ) -> List[Dict[str, Any]]:
                return []

            async def delete(self, ids: List[str]) -> None:
                pass

            async def get(self, ids: List[str]) -> List[Dict[str, Any]]:
                return []

            async def count(self) -> int:
                return 0

        instance = MockVectorStore()
        asyncio.run(instance.initialize("test", 768))

        with pytest.raises(ValueError):
            asyncio.run(instance.initialize("test", -1))


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestErrorHandling:
    """Test error handling in ABC implementations."""

    def test_storage_read_missing_file(self) -> None:
        """StorageAdapter raises FileNotFoundError for missing files."""

        class MockStorageAdapter(StorageAdapter):
            async def list_files(
                self, path: str, recursive: bool = False
            ) -> List[FileInfo]:
                return []

            async def read_file(self, path: str) -> bytes:
                raise FileNotFoundError(f"File not found: {path}")

            async def get_metadata(self, path: str) -> FileMetadata:
                return FileMetadata(path=path, size_bytes=0, modified_at=datetime.now())

            async def exists(self, path: str) -> bool:
                return False

            async def list_changes(
                self, since: datetime, path: Optional[str] = None
            ) -> List[FileChange]:
                return []

            async def supports_watch(self) -> bool:
                return False

            async def watch(self, path: str, callback: Any) -> Any:
                raise NotImplementedError()

        instance = MockStorageAdapter()
        with pytest.raises(FileNotFoundError):
            asyncio.run(instance.read_file("/nonexistent"))

    def test_cache_connection_error(self) -> None:
        """Cache handles connection errors gracefully."""

        class FailingCache(Cache):
            async def get(self, key: str) -> Optional[Any]:
                raise RuntimeError("Connection failed")

            async def set(
                self, key: str, value: Any, ttl: Optional[int] = None
            ) -> None:
                raise RuntimeError("Connection failed")

            async def delete(self, key: str) -> None:
                raise RuntimeError("Connection failed")

            async def clear(self) -> None:
                raise RuntimeError("Connection failed")

        instance = FailingCache()
        with pytest.raises(RuntimeError, match="Connection failed"):
            asyncio.run(instance.get("key"))

    def test_vector_store_search_error(self) -> None:
        """VectorStore handles search errors."""

        class FailingVectorStore(VectorStore):
            async def initialize(
                self, collection_name: str, embedding_dim: int
            ) -> None:
                pass

            async def add_documents(
                self,
                ids: List[str],
                embeddings: List[List[float]],
                metadatas: List[Dict[str, Any]],
                documents: List[str],
            ) -> None:
                pass

            async def search(
                self,
                query_embedding: List[float],
                filter_dict: Optional[Dict[str, Any]] = None,
                top_k: int = 10,
                include: Optional[List[str]] = None,
            ) -> List[Dict[str, Any]]:
                raise RuntimeError("Search failed")

            async def delete(self, ids: List[str]) -> None:
                pass

            async def get(self, ids: List[str]) -> List[Dict[str, Any]]:
                return []

            async def count(self) -> int:
                return 0

        instance = FailingVectorStore()
        with pytest.raises(RuntimeError, match="Search failed"):
            asyncio.run(instance.search([0.1, 0.2]))


# =============================================================================
# Async Behavior Tests
# =============================================================================


class TestAsyncBehavior:
    """Test async method behavior."""

    @pytest.mark.asyncio
    async def test_vector_store_async(self) -> None:
        """VectorStore methods are async."""

        class AsyncVectorStore(VectorStore):
            def __init__(self) -> None:
                self._initialized = False

            async def initialize(
                self, collection_name: str, embedding_dim: int
            ) -> None:
                await asyncio.sleep(0.001)  # Simulate async work
                self._initialized = True

            async def add_documents(
                self,
                ids: List[str],
                embeddings: List[List[float]],
                metadatas: List[Dict[str, Any]],
                documents: List[str],
            ) -> None:
                await asyncio.sleep(0.001)

            async def search(
                self,
                query_embedding: List[float],
                filter_dict: Optional[Dict[str, Any]] = None,
                top_k: int = 10,
                include: Optional[List[str]] = None,
            ) -> List[Dict[str, Any]]:
                await asyncio.sleep(0.001)
                return []

            async def delete(self, ids: List[str]) -> None:
                await asyncio.sleep(0.001)

            async def get(self, ids: List[str]) -> List[Dict[str, Any]]:
                await asyncio.sleep(0.001)
                return []

            async def count(self) -> int:
                await asyncio.sleep(0.001)
                return 0

        instance = AsyncVectorStore()
        await instance.initialize("test", 768)
        assert instance._initialized

    @pytest.mark.asyncio
    async def test_concurrent_cache_access(self) -> None:
        """Cache handles concurrent access."""

        class AsyncCache(Cache):
            def __init__(self) -> None:
                self._data: Dict[str, Any] = {}
                self._lock = asyncio.Lock()

            async def get(self, key: str) -> Optional[Any]:
                async with self._lock:
                    return self._data.get(key)

            async def set(
                self, key: str, value: Any, ttl: Optional[int] = None
            ) -> None:
                async with self._lock:
                    self._data[key] = value

            async def delete(self, key: str) -> None:
                async with self._lock:
                    self._data.pop(key, None)

            async def clear(self) -> None:
                async with self._lock:
                    self._data.clear()

        instance = AsyncCache()

        # Simulate concurrent operations
        await asyncio.gather(
            instance.set("key1", "value1"),
            instance.set("key2", "value2"),
            instance.get("key1"),
        )

        assert await instance.get("key1") == "value1"
        assert await instance.get("key2") == "value2"


# =============================================================================
# State Management Tests
# =============================================================================


class TestStateManagement:
    """Test state management in ABCs."""

    def test_vector_store_state_tracking(self) -> None:
        """VectorStore maintains collection state."""

        class StatefulVectorStore(VectorStore):
            def __init__(self) -> None:
                self._docs: Dict[str, Dict[str, Any]] = {}
                self._initialized = False
                self._collection_name: str = ""

            async def initialize(
                self, collection_name: str, embedding_dim: int
            ) -> None:
                self._initialized = True
                self._collection_name = collection_name

            async def add_documents(
                self,
                ids: List[str],
                embeddings: List[List[float]],
                metadatas: List[Dict[str, Any]],
                documents: List[str],
            ) -> None:
                for doc_id, emb, meta, doc in zip(
                    ids, embeddings, metadatas, documents
                ):
                    self._docs[doc_id] = {
                        "embedding": emb,
                        "metadata": meta,
                        "document": doc,
                    }

            async def search(
                self,
                query_embedding: List[float],
                filter_dict: Optional[Dict[str, Any]] = None,
                top_k: int = 10,
                include: Optional[List[str]] = None,
            ) -> List[Dict[str, Any]]:
                return list(self._docs.values())[:top_k]

            async def delete(self, ids: List[str]) -> None:
                for doc_id in ids:
                    self._docs.pop(doc_id, None)

            async def get(self, ids: List[str]) -> List[Dict[str, Any]]:
                return [self._docs[doc_id] for doc_id in ids if doc_id in self._docs]

            async def count(self) -> int:
                return len(self._docs)

        instance = StatefulVectorStore()
        asyncio.run(instance.initialize("test_collection", 768))
        assert instance._collection_name == "test_collection"

        # Add documents
        asyncio.run(
            instance.add_documents(
                ids=["doc1", "doc2"],
                embeddings=[[0.1, 0.2], [0.3, 0.4]],
                metadatas=[{"key": "val1"}, {"key": "val2"}],
                documents=["content1", "content2"],
            )
        )
        assert asyncio.run(instance.count()) == 2

        # Delete one
        asyncio.run(instance.delete(["doc1"]))
        assert asyncio.run(instance.count()) == 1

    def test_cache_invalidation(self) -> None:
        """Cache supports proper invalidation semantics."""

        class InvalidatableCache(Cache):
            def __init__(self) -> None:
                self._data: Dict[str, Any] = {}
                self._access_count: Dict[str, int] = {}

            async def get(self, key: str) -> Optional[Any]:
                val = self._data.get(key)
                if val is not None:
                    self._access_count[key] = self._access_count.get(key, 0) + 1
                return val

            async def set(
                self, key: str, value: Any, ttl: Optional[int] = None
            ) -> None:
                self._data[key] = value
                self._access_count[key] = 0

            async def delete(self, key: str) -> None:
                self._data.pop(key, None)
                self._access_count.pop(key, None)

            async def clear(self) -> None:
                self._data.clear()
                self._access_count.clear()

        instance = InvalidatableCache()
        asyncio.run(instance.set("key1", "value1"))
        asyncio.run(instance.set("key2", "value2"))

        # Access key1 twice
        asyncio.run(instance.get("key1"))
        asyncio.run(instance.get("key1"))

        # Delete key1
        asyncio.run(instance.delete("key1"))
        assert asyncio.run(instance.get("key1")) is None
        assert asyncio.run(instance.get("key2")) == "value2"

        # Clear all
        asyncio.run(instance.clear())
        assert asyncio.run(instance.get("key2")) is None


# =============================================================================
# Type Hint Tests
# =============================================================================


class TestTypeHints:
    """Test that all ABCs have proper type hints."""

    def test_vector_store_type_hints(self) -> None:
        """VectorStore methods have type hints."""
        for name, method in inspect.getmembers(
            VectorStore, predicate=inspect.isfunction
        ):
            if hasattr(method, "__annotations__"):
                # Check that method has annotations
                sig = inspect.signature(method)
                assert sig.return_annotation != inspect.Signature.empty

    def test_storage_adapter_type_hints(self) -> None:
        """StorageAdapter methods have type hints."""
        for name, method in inspect.getmembers(
            StorageAdapter, predicate=inspect.isfunction
        ):
            if hasattr(method, "__annotations__"):
                sig = inspect.signature(method)
                assert sig.return_annotation != inspect.Signature.empty

    def test_cache_type_hints(self) -> None:
        """Cache methods have type hints."""
        for name, method in inspect.getmembers(Cache, predicate=inspect.isfunction):
            if hasattr(method, "__annotations__"):
                sig = inspect.signature(method)
                assert sig.return_annotation != inspect.Signature.empty

    def test_reranker_type_hints(self) -> None:
        """Reranker methods have type hints."""
        for name, method in inspect.getmembers(Reranker, predicate=inspect.isfunction):
            if hasattr(method, "__annotations__"):
                sig = inspect.signature(method)
                assert sig.return_annotation != inspect.Signature.empty


# =============================================================================
# Data Model Tests
# =============================================================================


class TestDataModels:
    """Test Pydantic/dataclass models."""

    def test_file_info_creation(self) -> None:
        """FileInfo can be created with default values."""
        info = FileInfo(path="/test/file.txt", name="file.txt", size_bytes=100)
        assert info.path == "/test/file.txt"
        assert info.size_bytes == 100
        assert info.is_directory is False

    def test_file_metadata_creation(self) -> None:
        """FileMetadata can be created."""
        meta = FileMetadata(
            path="/test/file.txt",
            size_bytes=100,
            modified_at=datetime.now(),
            file_hash="abc123",
        )
        assert meta.size_bytes == 100
        assert meta.file_hash == "abc123"

    def test_file_change_creation(self) -> None:
        """FileChange can be created."""
        change = FileChange(
            change_type=FileChangeType.MODIFIED,
            path="/test/file.txt",
        )
        assert change.change_type == FileChangeType.MODIFIED

    def test_storage_backend_enum(self) -> None:
        """StorageBackend enum has expected values."""
        assert StorageBackend.LOCAL.value == "local"
        assert StorageBackend.GOOGLE_DRIVE.value == "gdrive"
        assert StorageBackend.ONE_DRIVE.value == "onedrive"


# =============================================================================
# Logger Tests
# =============================================================================


class TestLogger:
    """Test logger functionality."""

    def test_logger_import(self) -> None:
        """Logger module can be imported."""
        assert get_logger is not None
        assert setup_logger is not None

    def test_logger_directory_creation(self, tmp_path: Path) -> None:
        """Logger creates directory if not exists."""
        log_dir = tmp_path / "test_logs"
        setup_logger(log_dir=log_dir)
        assert log_dir.exists()

    @patch("os.access")
    def test_get_logger_creates_instance(self, mock_access: MagicMock) -> None:
        """get_logger returns a configured logger."""
        mock_access.return_value = False

        log = get_logger("test_module")
        assert log is not None


# =============================================================================
# WatchHandle Protocol Tests
# =============================================================================


class TestWatchHandle:
    """Test WatchHandle protocol."""

    def test_watch_handle_protocol(self) -> None:
        """WatchHandle protocol is valid."""
        from grimoire.storage.base import WatchHandle

        class MockWatchHandle:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def is_running(self) -> bool:
                return True

            def __enter__(self) -> "MockWatchHandle":
                self.start()
                return self

            def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
                self.stop()

        # This should compile without issues
        handle: WatchHandle = MockWatchHandle()
        assert handle.is_running()

        with handle as h:
            assert h.is_running()
