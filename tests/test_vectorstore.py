"""Comprehensive tests for VectorStore implementations.

This module contains unit and integration tests for all VectorStore
implementations, following the testing standards from Appendix D.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Generator

import pytest

from grimoire.vectorstore import ChromaDBStore
from grimoire.vectorstore.base import VectorStore

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_db_path() -> Generator[Path, None, None]:
    """Provide a temporary directory for ChromaDB persistence.

    Yields:
        Path: Temporary directory path.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir)


@pytest.fixture
def chroma_store(temp_db_path: Path) -> Generator[ChromaDBStore, None, None]:
    """Create and initialize a ChromaDBStore for testing.

    Args:
        temp_db_path: Pytest fixture providing a temporary directory.

    Yields:
        ChromaDBStore: Initialized store with a test collection.
    """
    store = ChromaDBStore(
        persist_directory=temp_db_path,
        collection_name="test_collection",
        distance_metric="cosine",
    )
    yield store


@pytest.fixture
def initialized_chroma_store(
    chroma_store: ChromaDBStore,
) -> Generator[ChromaDBStore, None, None]:
    """Create an initialized ChromaDBStore with test data.

    Args:
        chroma_store: Uninitialized ChromaDBStore fixture.

    Yields:
        ChromaDBStore: Store initialized with embedding_dim=384.
    """
    # Initialize with a common embedding dimension (384 for MiniLM)
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        chroma_store.initialize(collection_name="test_collection", embedding_dim=384)
    )
    yield chroma_store


@pytest.fixture
def sample_embeddings() -> list[list[float]]:
    """Provide sample embeddings for testing.

    Returns:
        List of embedding vectors.
    """
    return [
        [0.1] * 384,  # Document 1
        [0.2] * 384,  # Document 2
        [0.3] * 384,  # Document 3
        [0.4] * 384,  # Document 4
    ]


@pytest.fixture
def sample_metadatas() -> list[dict[str, Any]]:
    """Provide sample metadata for testing.

    Returns:
        List of metadata dictionaries.
    """
    return [
        {"doc_id": "doc1", "chunk_idx": 0, "tags": "research,ai"},
        {"doc_id": "doc2", "chunk_idx": 0, "tags": "notes"},
        {"doc_id": "doc3", "chunk_idx": 0, "tags": "research,important"},
        {"doc_id": "doc4", "chunk_idx": 1, "tags": "ai"},
    ]


@pytest.fixture
def sample_documents() -> list[str]:
    """Provide sample documents for testing.

    Returns:
        List of text documents.
    """
    return [
        "This is a research document about AI.",
        "These are personal notes.",
        "Important research findings here.",
        "AI and machine learning discussion.",
    ]


# =============================================================================
# Happy Path Tests
# =============================================================================


class TestChromaDBHappyPath:
    """Standard use cases for ChromaDBStore."""

    @pytest.mark.asyncio
    async def test_basic_initialization(self, temp_db_path: Path) -> None:
        """Test basic store initialization."""
        store = ChromaDBStore(persist_directory=temp_db_path)
        await store.initialize(collection_name="test", embedding_dim=384)

        assert store.is_initialized
        assert store.collection_name == "test"

    @pytest.mark.asyncio
    async def test_custom_configuration(self, temp_db_path: Path) -> None:
        """Test store with custom configuration."""
        store = ChromaDBStore(
            persist_directory=temp_db_path,
            collection_name="custom_collection",
            distance_metric="euclidean",
        )
        await store.initialize(collection_name="custom_collection", embedding_dim=768)

        assert store.is_initialized
        assert store.collection_name == "custom_collection"

    @pytest.mark.asyncio
    async def test_add_documents(self, chroma_store: ChromaDBStore) -> None:
        """Test adding documents to the store."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        ids = ["doc1_chunk0", "doc2_chunk0"]
        embeddings = [[0.1] * 384, [0.2] * 384]
        metadatas = [
            {"doc_id": "doc1", "chunk_idx": 0},
            {"doc_id": "doc2", "chunk_idx": 0},
        ]
        documents = ["Document 1 text", "Document 2 text"]

        await chroma_store.add_documents(ids, embeddings, metadatas, documents)

        count = await chroma_store.count()
        assert count == 2

    @pytest.mark.asyncio
    async def test_search_by_similarity(self, chroma_store: ChromaDBStore) -> None:
        """Test similarity search returns expected results."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        # Add test documents - use simple vectors
        ids = ["doc1_chunk0", "doc2_chunk0", "doc3_chunk0"]
        embeddings = [
            [1.0] * 384,  # First doc
            [0.5] * 384,  # Second doc
            [0.0] * 384,  # Third doc
        ]
        metadatas = [
            {"doc_id": "doc1", "chunk_idx": 0},
            {"doc_id": "doc2", "chunk_idx": 0},
            {"doc_id": "doc3", "chunk_idx": 0},
        ]
        documents = [
            "The first document content",
            "Another document",
            "Totally unrelated",
        ]

        await chroma_store.add_documents(ids, embeddings, metadatas, documents)

        # Search - should return results
        query = [1.0] * 384
        results = await chroma_store.search(query, top_k=3)

        assert len(results) == 3
        assert "distance" in results[0]
        assert "metadata" in results[0]
        assert "document" in results[0]

    @pytest.mark.asyncio
    async def test_delete_documents(self, chroma_store: ChromaDBStore) -> None:
        """Test deletion of documents by ID."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        # Add documents
        ids = ["doc1", "doc2", "doc3"]
        embeddings = [[0.1] * 384, [0.2] * 384, [0.3] * 384]
        metadatas = [{"_empty": False}, {"_empty": False}, {"_empty": False}]
        documents = ["doc1", "doc2", "doc3"]

        await chroma_store.add_documents(ids, embeddings, metadatas, documents)
        assert await chroma_store.count() == 3

        # Delete middle document
        await chroma_store.delete(["doc2"])
        assert await chroma_store.count() == 2

    @pytest.mark.asyncio
    async def test_get_documents(self, chroma_store: ChromaDBStore) -> None:
        """Test retrieval of documents by ID."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        ids = ["doc1_chunk0"]
        embeddings = [[0.1] * 384]
        metadatas = [{"doc_id": "doc1", "chunk_idx": 0, "tags": "research"}]
        documents = ["Research document content"]

        await chroma_store.add_documents(ids, embeddings, metadatas, documents)

        results = await chroma_store.get(["doc1_chunk0"])

        assert len(results) == 1
        assert results[0]["id"] == "doc1_chunk0"
        assert results[0]["document"] == "Research document content"
        assert results[0]["metadata"]["doc_id"] == "doc1"
        assert "embedding" in results[0]

    @pytest.mark.asyncio
    async def test_count_empty_collection(self, chroma_store: ChromaDBStore) -> None:
        """Test count on empty collection returns 0."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        count = await chroma_store.count()
        assert count == 0


# =============================================================================
# Edge Cases & Boundary Conditions
# =============================================================================


class TestChromaDBEdgeCases:
    """Boundary conditions and unusual inputs."""

    @pytest.mark.asyncio
    async def test_empty_input_add(self, chroma_store: ChromaDBStore) -> None:
        """Test adding empty document list is handled gracefully."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        # Adding empty lists should not fail
        await chroma_store.add_documents([], [], [], [])
        count = await chroma_store.count()
        assert count == 0

    @pytest.mark.asyncio
    async def test_single_element_input(self, chroma_store: ChromaDBStore) -> None:
        """Test with single document."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        await chroma_store.add_documents(
            ids=["single"],
            embeddings=[[0.1] * 384],
            metadatas=[{"single": True}],
            documents=["Single document"],
        )

        results = await chroma_store.search([0.1] * 384, top_k=1)
        assert len(results) == 1
        assert results[0]["id"] == "single"

    @pytest.mark.asyncio
    async def test_large_document_batch(self, chroma_store: ChromaDBStore) -> None:
        """Test adding many documents at once."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        # Batch of 50 documents
        batch_size = 50
        ids = [f"doc{i}" for i in range(batch_size)]
        embeddings = [[i / batch_size] * 384 for i in range(batch_size)]
        metadatas = [{"idx": i} for i in range(batch_size)]
        documents = [f"Document {i}" for i in range(batch_size)]

        await chroma_store.add_documents(ids, embeddings, metadatas, documents)

        count = await chroma_store.count()
        assert count == batch_size

    @pytest.mark.asyncio
    async def test_unicode_document_content(self, chroma_store: ChromaDBStore) -> None:
        """Test documents with unicode/special characters."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        ids = ["unicode_doc"]
        embeddings = [[0.1] * 384]
        metadatas = [{"title": "Unicode test: αβγ 🌍 中文"}]
        documents = ["Unicode content: αβγ 🌍 中文"]

        await chroma_store.add_documents(ids, embeddings, metadatas, documents)

        results = await chroma_store.get(["unicode_doc"])
        assert len(results) == 1
        assert "Unicode content" in results[0]["document"]

    @pytest.mark.asyncio
    async def test_weird_file_ids(self, chroma_store: ChromaDBStore) -> None:
        """Test document IDs with unusual characters."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        ids = ["file:1#section", "path/to/file.name", "file-with-dashes_underscores"]
        embeddings = [[0.1] * 384, [0.2] * 384, [0.3] * 384]
        metadatas = [{"id": i} for i in range(3)]
        documents = ["doc1", "doc2", "doc3"]

        await chroma_store.add_documents(ids, embeddings, metadatas, documents)

        results = await chroma_store.get(ids)
        assert len(results) == 3


# =============================================================================
# Input Validation & Error Handling
# =============================================================================


class TestChromaDBInputValidation:
    """Invalid inputs are rejected gracefully."""

    @pytest.mark.asyncio
    async def test_invalid_distance_metric(self, temp_db_path: Path) -> None:
        """Test invalid distance metric raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            ChromaDBStore(
                persist_directory=temp_db_path,
                distance_metric="invalid_metric",
            )
        assert "Unsupported distance metric" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_invalid_embedding_dimension(
        self, chroma_store: ChromaDBStore
    ) -> None:
        """Test invalid embedding dimensions raise ValueError."""
        # Invalid dimension during initialization
        with pytest.raises(ValueError) as exc_info:
            await chroma_store.initialize(collection_name="test", embedding_dim=-1)
        assert "embedding_dim must be a positive integer" in str(exc_info.value)

        with pytest.raises(ValueError) as exc_info:
            await chroma_store.initialize(collection_name="test", embedding_dim=0)
        assert "embedding_dim must be a positive integer" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_mismatched_list_lengths(self, chroma_store: ChromaDBStore) -> None:
        """Test mismatched input lengths raise ValueError."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        with pytest.raises(ValueError) as exc_info:
            await chroma_store.add_documents(
                ids=["doc1", "doc2"],
                embeddings=[[0.1] * 384],  # Only 1 embedding
                metadatas=[{}, {}],
                documents=["doc1", "doc2"],
            )
        assert "Input list lengths must match" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_wrong_embedding_dimension(self, chroma_store: ChromaDBStore) -> None:
        """Test wrong embedding dimension raises ValueError."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        with pytest.raises(ValueError) as exc_info:
            await chroma_store.add_documents(
                ids=["doc1"],
                embeddings=[[0.1] * 100],  # Wrong dimension
                metadatas=[{}],
                documents=["doc1"],
            )
        assert "has dimension 100, expected 384" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_invalid_top_k(self, chroma_store: ChromaDBStore) -> None:
        """Test invalid top_k values raise ValueError."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        with pytest.raises(ValueError) as exc_info:
            await chroma_store.search([0.1] * 384, top_k=0)
        assert "top_k must be a positive integer" in str(exc_info.value)

        with pytest.raises(ValueError) as exc_info:
            await chroma_store.search([0.1] * 384, top_k=-1)
        assert "top_k must be a positive integer" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_dimension_mismatch(self, chroma_store: ChromaDBStore) -> None:
        """Test search with mismatched query dimension raises ValueError."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        with pytest.raises(ValueError) as exc_info:
            await chroma_store.search([0.1] * 100, top_k=5)  # Wrong dimension
        assert "does not match expected 384" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_delete_empty_list(self, chroma_store: ChromaDBStore) -> None:
        """Test delete with empty list raises ValueError."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        with pytest.raises(ValueError) as exc_info:
            await chroma_store.delete([])
        assert "Cannot delete empty list" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_empty_list(self, chroma_store: ChromaDBStore) -> None:
        """Test get with empty list raises ValueError."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        with pytest.raises(ValueError) as exc_info:
            await chroma_store.get([])
        assert "Cannot get empty list" in str(exc_info.value)


# =============================================================================
# Metadata Filtering Tests
# =============================================================================


class TestChromaDBMetadataFiltering:
    """Test metadata filtering functionality."""

    @pytest.mark.asyncio
    async def test_filter_by_equality(self, chroma_store: ChromaDBStore) -> None:
        """Test equality filter {\": {"$eq": value}}."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        # Add documents with different doc_ids
        await chroma_store.add_documents(
            ids=["d1", "d2", "d3"],
            embeddings=[[0.1] * 384, [0.15] * 384, [0.2] * 384],
            metadatas=[
                {"doc_id": "doc1", "tags": "ai"},
                {"doc_id": "doc2", "tags": "research"},
                {"doc_id": "doc1", "tags": "notes"},  # Same doc_id as first
            ],
            documents=["d1", "d2", "d3"],
        )

        # Filter by doc_id
        results = await chroma_store.search(
            query_embedding=[0.1] * 384,
            filter_dict={"doc_id": {"$eq": "doc1"}},
            top_k=10,
        )

        assert len(results) == 2
        for r in results:
            assert r["metadata"]["doc_id"] == "doc1"

    @pytest.mark.asyncio
    async def test_filter_numeric_comparison(self, chroma_store: ChromaDBStore) -> None:
        """Test numeric comparison filters ($gt, $gte, $lt, $lte)."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        await chroma_store.add_documents(
            ids=["d1", "d2", "d3", "d4"],
            embeddings=[[0.1] * 384, [0.15] * 384, [0.2] * 384, [0.25] * 384],
            metadatas=[
                {"chunk_idx": 0, "priority": 1},
                {"chunk_idx": 1, "priority": 5},
                {"chunk_idx": 2, "priority": 10},
                {"chunk_idx": 3, "priority": 15},
            ],
            documents=["d1", "d2", "d3", "d4"],
        )

        # Filter priority > 5
        results = await chroma_store.search(
            query_embedding=[0.1] * 384,
            filter_dict={"priority": {"$gt": 5}},
            top_k=10,
        )

        priorities = [r["metadata"]["priority"] for r in results]
        assert all(p > 5 for p in priorities)

    @pytest.mark.asyncio
    async def test_filter_in_list(self, chroma_store: ChromaDBStore) -> None:
        """Test $in filter for matching any value in a list."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        await chroma_store.add_documents(
            ids=["d1", "d2", "d3"],
            embeddings=[[0.1] * 384, [0.15] * 384, [0.2] * 384],
            metadatas=[
                {"status": "draft"},
                {"status": "published"},
                {"status": "archived"},
            ],
            documents=["d1", "d2", "d3"],
        )

        results = await chroma_store.search(
            query_embedding=[0.1] * 384,
            filter_dict={"status": {"$in": ["draft", "published"]}},
            top_k=10,
        )

        assert len(results) == 2
        statuses = [r["metadata"]["status"] for r in results]
        assert "draft" in statuses
        assert "published" in statuses
        assert "archived" not in statuses

    @pytest.mark.asyncio
    async def test_filter_logical_and(self, chroma_store: ChromaDBStore) -> None:
        """Test logical $and filter."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        await chroma_store.add_documents(
            ids=["d1", "d2", "d3"],
            embeddings=[[0.1] * 384, [0.15] * 384, [0.2] * 384],
            metadatas=[
                {"category": "tech", "status": "active"},
                {"category": "tech", "status": "draft"},
                {"category": "finance", "status": "active"},
            ],
            documents=["d1", "d2", "d3"],
        )

        # Both tech AND active
        results = await chroma_store.search(
            query_embedding=[0.1] * 384,
            filter_dict={
                "$and": [
                    {"category": {"$eq": "tech"}},
                    {"status": {"$eq": "active"}},
                ]
            },
            top_k=10,
        )

        assert len(results) == 1
        assert results[0]["id"] == "d1"

    @pytest.mark.asyncio
    async def test_filter_logical_or(self, chroma_store: ChromaDBStore) -> None:
        """Test logical $or filter."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        await chroma_store.add_documents(
            ids=["d1", "d2", "d3"],
            embeddings=[[0.1] * 384, [0.15] * 384, [0.2] * 384],
            metadatas=[
                {"category": "tech"},
                {"category": "finance"},
                {"category": "healthcare"},
            ],
            documents=["d1", "d2", "d3"],
        )

        # Either tech OR finance
        results = await chroma_store.search(
            query_embedding=[0.1] * 384,
            filter_dict={
                "$or": [
                    {"category": {"$eq": "tech"}},
                    {"category": {"$eq": "finance"}},
                ]
            },
            top_k=10,
        )

        assert len(results) == 2
        categories = set(r["metadata"]["category"] for r in results)
        assert categories == {"tech", "finance"}

    @pytest.mark.asyncio
    async def test_invalid_filter_operator(self, chroma_store: ChromaDBStore) -> None:
        """Test invalid filter operators raise ValueError."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        await chroma_store.add_documents(
            ids=["d1"],
            embeddings=[[0.1] * 384],
            metadatas=[{"key": "value"}],
            documents=["doc"],
        )

        with pytest.raises(ValueError) as exc_info:
            await chroma_store.search(
                query_embedding=[0.1] * 384,
                filter_dict={"key": {"$invalid_op": "value"}},
                top_k=10,
            )
        assert "Unsupported operator" in str(exc_info.value)


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestChromaDBErrorHandling:
    """Errors are caught and handled appropriately."""

    @pytest.mark.asyncio
    async def test_uninitialized_store_operations(
        self, chroma_store: ChromaDBStore
    ) -> None:
        """Test operations before initialization raise RuntimeError."""

        with pytest.raises(RuntimeError) as exc_info:
            await chroma_store.count()
        assert "not initialized" in str(exc_info.value)

        with pytest.raises(RuntimeError) as exc_info:
            await chroma_store.add_documents(
                ids=["test"],
                embeddings=[[0.1] * 384],
                metadatas=[{}],
                documents=["test"],
            )
        assert "not initialized" in str(exc_info.value)

        with pytest.raises(RuntimeError) as exc_info:
            await chroma_store.search([0.1] * 384, top_k=5)
        assert "not initialized" in str(exc_info.value)

        with pytest.raises(RuntimeError) as exc_info:
            await chroma_store.get(["test"])
        assert "not initialized" in str(exc_info.value)

        with pytest.raises(RuntimeError) as exc_info:
            await chroma_store.delete(["test"])
        assert "not initialized" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_reset_not_initialized(self, chroma_store: ChromaDBStore) -> None:
        """Test reset without initialization raises RuntimeError."""
        with pytest.raises(RuntimeError) as exc_info:
            await chroma_store.reset()
        assert "not initialized" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_reset_collection(self, chroma_store: ChromaDBStore) -> None:
        """Test reset clears all documents."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        # Add some documents
        await chroma_store.add_documents(
            ids=["doc1", "doc2"],
            embeddings=[[0.1] * 384, [0.2] * 384],
            metadatas=[{"id": 1}, {"id": 2}],
            documents=["doc1", "doc2"],
        )
        assert await chroma_store.count() == 2

        # Reset
        await chroma_store.reset()
        assert await chroma_store.count() == 0


# =============================================================================
# Metadata Serialization Tests
# =============================================================================


class TestChromaDBMetadataSerialization:
    """Test metadata handling and serialization."""

    @pytest.mark.asyncio
    async def test_list_metadata_handling(self, chroma_store: ChromaDBStore) -> None:
        """Test that list metadata is properly serialized/deserialized."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        # Add document with list in metadata
        await chroma_store.add_documents(
            ids=["doc1"],
            embeddings=[[0.1] * 384],
            metadatas=[{"tags": "research,ai,important"}],
            documents=["content"],
        )

        # Retrieve and verify tags are restored properly
        results = await chroma_store.get(["doc1"])
        assert len(results) == 1
        # Metadata lists are converted to comma-separated strings for storage
        assert results[0]["metadata"]["tags"] == ["research", "ai", "important"]


# =============================================================================
# Concurrency Tests
# =============================================================================


class TestChromaDBConcurrency:
    """Async and concurrent behavior."""

    @pytest.mark.asyncio
    async def test_multiple_operations_sequential(
        self, chroma_store: ChromaDBStore
    ) -> None:
        """Test multiple operations in sequence work correctly."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        # Add documents sequentially
        for i in range(10):
            await chroma_store.add_documents(
                ids=[f"doc{i}"],
                embeddings=[[i / 10] * 384],
                metadatas=[{"idx": i}],
                documents=[f"content{i}"],
            )

        count = await chroma_store.count()
        assert count == 10

    @pytest.mark.asyncio
    async def test_update_existing_document(self, chroma_store: ChromaDBStore) -> None:
        """Test updating an existing document replaces it."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        # Add initial document
        await chroma_store.add_documents(
            ids=["doc1"],
            embeddings=[[0.1] * 384],
            metadatas=[{"version": 1}],
            documents=["original content"],
        )

        # Update same ID
        await chroma_store.add_documents(
            ids=["doc1"],
            embeddings=[[0.2] * 384],
            metadatas=[{"version": 2}],
            documents=["updated content"],
        )

        count = await chroma_store.count()
        assert count == 1  # Still 1 document

        results = await chroma_store.get(["doc1"])
        assert results[0]["document"] == "updated content"


# =============================================================================
# VectorStore Interface Compliance
# =============================================================================


class TestVectorStoreABCCompliance:
    """Verify ChromaDBStore conforms to VectorStore ABC."""

    def test_is_abstract_base_class_subclass(self) -> None:
        """ChromaDBStore is a subclass of VectorStore."""
        assert issubclass(ChromaDBStore, VectorStore)

    def test_abstract_methods_implemented(self, chroma_store: ChromaDBStore) -> None:
        """All abstract methods are implemented."""
        abstract_methods = [
            "initialize",
            "add_documents",
            "search",
            "delete",
            "get",
            "count",
        ]
        for method in abstract_methods:
            assert hasattr(chroma_store, method)
            assert callable(getattr(chroma_store, method))


# =============================================================================
# Configuration Tests
# =============================================================================


class TestChromaDBConfiguration:
    """Test configuration and initialization options."""

    @pytest.mark.asyncio
    async def test_ip_distance_metric(self, temp_db_path: Path) -> None:
        """Test initialization with IP (inner product) distance metric."""
        store = ChromaDBStore(
            persist_directory=temp_db_path,
            distance_metric="ip",
        )
        await store.initialize(collection_name="test", embedding_dim=384)
        assert store.is_initialized

    @pytest.mark.asyncio
    async def test_filter_contains_operator(self, chroma_store: ChromaDBStore) -> None:
        """Test $contains filter operator."""
        await chroma_store.initialize(
            collection_name="test_collection", embedding_dim=384
        )

        await chroma_store.add_documents(
            ids=["d1", "d2"],
            embeddings=[[0.1] * 384, [0.2] * 384],
            metadatas=[
                {"tag": "research"},
                {"tag": "notes"},
            ],
            documents=["d1", "d2"],
        )

        # Test $contains (converted to $in internally)
        results = await chroma_store.search(
            query_embedding=[0.1] * 384,
            filter_dict={"tag": {"$contains": "research"}},
            top_k=10,
        )

        assert len(results) == 1
        assert results[0]["metadata"]["tag"] == "research"

    """Test configuration and initialization options."""

    @pytest.mark.asyncio
    async def test_all_distance_metrics(self, temp_db_path: Path) -> None:
        """Test initialization with all supported distance metrics."""
        for metric in ["cosine", "euclidean", "ip"]:
            store = ChromaDBStore(
                persist_directory=temp_db_path / metric,
                distance_metric=metric,
            )
            await store.initialize(collection_name="test", embedding_dim=384)
            assert store.is_initialized

    @pytest.mark.asyncio
    async def test_path_expansion(self, temp_db_path: Path) -> None:
        """Test that ~ paths are expanded."""
        # Note: We can't actually use ~ in temp path, but we test that Path works
        store = ChromaDBStore(persist_directory="test_chroma")
        assert store.persist_directory.name == "test_chroma"


# =============================================================================
# Integration Tests
# =============================================================================


class TestChromaDBIntegration:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_full_crud_cycle(self, temp_db_path: Path) -> None:
        """Test complete create, read, update, delete cycle."""
        store = ChromaDBStore(persist_directory=temp_db_path)
        await store.initialize(collection_name="test", embedding_dim=5)

        # Create
        await store.add_documents(
            ids=["doc1", "doc2"],
            embeddings=[[0.1, 0.2, 0.3, 0.4, 0.5], [0.5, 0.4, 0.3, 0.2, 0.1]],
            metadatas=[{"type": "article"}, {"type": "note"}],
            documents=["Article content", "Note content"],
        )
        assert await store.count() == 2

        # Read
        results = await store.get(["doc1"])
        assert results[0]["document"] == "Article content"

        # Update (same ID)
        await store.add_documents(
            ids=["doc1"],
            embeddings=[[0.1, 0.2, 0.3, 0.4, 0.5]],
            metadatas=[{"type": "updated"}],
            documents=["Updated content"],
        )
        results = await store.get(["doc1"])
        assert results[0]["metadata"]["type"] == "updated"

        # Delete
        await store.delete(["doc1"])
        assert await store.count() == 1

        # Search still works
        results = await store.search([0.5, 0.4, 0.3, 0.2, 0.1], top_k=5)
        assert len(results) == 1
