"""ChromaDB VectorStore implementation for Grimoire.

This module provides an implementation of the VectorStore ABC using ChromaDB
as the backend. It supports metadata filtering, multiple distance metrics,
and proper initialization with metadata schema.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union, cast

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import ChromaError, NotFoundError
from loguru import logger

from grimoire.vectorstore.base import VectorStore


class ChromaDBStore(VectorStore):
    """ChromaDB implementation of the VectorStore ABC.

    This implementation uses ChromaDB's native metadata filtering and
    supports multiple distance metrics (cosine, euclidean, ip).

    Example:
        >>> store = ChromaDBStore("./chroma_db")
        >>> await store.initialize("documents", embedding_dim=768)
        >>> await store.add_documents(
        ...     ids=["1"],
        ...     embeddings=[[0.1, 0.2, ...]],
        ...     metadatas=[{"doc_id": "doc1", "chunk_idx": 0}],
        ...     documents=["text content"]
        ... )

    Attributes:
        persist_directory: Path where ChromaDB stores data.
        collection_name: Name of the active collection.
        distance_metric: Distance function used for similarity search.
        client: ChromaDB client instance.
        collection: Active ChromaDB collection.
    """

    # Note: ChromaDB uses 'l2' for Euclidean distance internally
    SUPPORTED_DISTANCE_METRICS = ["cosine", "euclidean", "ip"]
    DEFAULT_DISTANCE_METRIC = "cosine"

    def __init__(
        self,
        persist_directory: Union[str, Path],
        collection_name: str = "documents",
        distance_metric: str = "cosine",
        anonymized_telemetry: bool = False,
    ):
        """Initialize ChromaDBStore with configuration.

        Args:
            persist_directory: Path for ChromaDB persistence.
            collection_name: Name of the collection (default: "documents").
            distance_metric: Distance function (cosine, euclidean, ip).
            anonymized_telemetry: Whether to enable ChromaDB telemetry.

        Raises:
            ValueError: If distance_metric is not supported.
        """
        self.persist_directory = Path(os.path.expanduser(persist_directory))
        self.collection_name = collection_name
        self.distance_metric = self._validate_distance_metric(distance_metric)
        self.anonymized_telemetry = anonymized_telemetry
        self._client: Optional[ClientAPI] = None
        self._collection: Optional[Collection] = None
        self._embedding_dim: int = 0

    def _validate_distance_metric(self, metric: str) -> str:
        """Validate and normalize distance metric.

        Args:
            metric: Distance metric string.

        Returns:
            Normalized metric name for internal ChromaDB use.

        Raises:
            ValueError: If metric is not supported.
        """
        metric = metric.lower()
        if metric == "euclidean":
            return "l2"  # ChromaDB uses 'l2' for Euclidean distance
        if metric not in self.SUPPORTED_DISTANCE_METRICS:
            raise ValueError(
                f"Unsupported distance metric: {metric}. "
                f"Supported: {', '.join(['cosine', 'euclidean', 'ip'])}"
            )
        return metric

    async def initialize(self, collection_name: str, embedding_dim: int) -> None:
        """Initialize ChromaDB client and collection.

        Creates the persistence directory if it doesn't exist and initializes
        or retrieves the specified collection with proper metadata schema.

        Args:
            collection_name: Name of the collection to create/use.
            embedding_dim: Dimension of embedding vectors.

        Raises:
            ConnectionError: If unable to connect to ChromaDB.
            ValueError: If embedding_dim is invalid.
        """
        if not isinstance(embedding_dim, int) or embedding_dim <= 0:
            raise ValueError(
                f"embedding_dim must be a positive integer, got {embedding_dim}"
            )

        self._embedding_dim = embedding_dim
        self.collection_name = collection_name

        try:
            # Ensure persistence directory exists
            self.persist_directory.mkdir(parents=True, exist_ok=True)
            logger.debug(f"ChromaDB persistence directory: {self.persist_directory}")

            # Initialize ChromaDB client with settings
            settings = ChromaSettings(
                is_persistent=True,
                persist_directory=str(self.persist_directory),
                anonymized_telemetry=self.anonymized_telemetry,
            )

            self._client = chromadb.Client(settings=settings)
            logger.info(f"ChromaDB client initialized at {self.persist_directory}")

            # Get or create collection with metadata schema
            # metadata is only used when creating a new collection
            metadata = {"hnsw:space": self.distance_metric}
            try:
                self._collection = self._client.get_or_create_collection(
                    name=collection_name,
                    metadata=metadata,
                    embedding_function=None,  # We provide embeddings explicitly
                )
            except Exception:
                # If collection exists with different settings, just get it
                self._collection = self._client.get_collection(
                    name=collection_name,
                )
            logger.info(
                f"Collection '{collection_name}' ready with "
                f"{self.distance_metric} distance metric"
            )

        except ChromaError as e:
            msg = f"Failed to initialize ChromaDB: {e}"
            logger.error(msg)
            raise ConnectionError(msg) from e
        except OSError as e:
            msg = f"Failed to create persistence directory: {e}"
            logger.error(msg)
            raise ConnectionError(msg) from e

    async def add_documents(
        self,
        ids: List[str],
        embeddings: List[List[float]],
        metadatas: List[Dict[str, Any]],
        documents: List[str],
    ) -> None:
        """Add or update documents in the vector store.

        Args:
            ids: Unique identifiers for documents.
            embeddings: Vector embeddings for each document.
            metadatas: Metadata dictionaries for each document.
            documents: Original text content for each document.

        Raises:
            ValueError: If input lists have mismatched lengths.
            RuntimeError: If storage operation fails.
            RuntimeError: If store is not initialized.
        """
        if self._collection is None:
            raise RuntimeError(
                "ChromaDBStore not initialized. Call initialize() first."
            )

        # Validate input lengths
        if not (len(ids) == len(embeddings) == len(metadatas) == len(documents)):
            raise ValueError(
                f"Input list lengths must match: "
                f"ids={len(ids)}, embeddings={len(embeddings)}, "
                f"metadatas={len(metadatas)}, documents={len(documents)}"
            )

        if not ids:
            logger.debug("add_documents called with empty lists, nothing to add")
            return

        # Validate embedding dimensions
        for i, emb in enumerate(embeddings):
            if len(emb) != self._embedding_dim:
                raise ValueError(
                    f"Embedding at index {i} has dimension {len(emb)}, "
                    f"expected {self._embedding_dim}"
                )

        # Normalize metadata for ChromaDB compatibility
        # ChromaDB requires at least one metadata attribute per document
        normalized_metadatas: List[Dict[str, Union[str, int, float, bool]]] = []
        for meta in metadatas:
            if not meta:
                # ChromaDB doesn't allow empty metadata dictionaries
                meta = {"_empty": True}
            normalized_metadatas.append(self._normalize_single_metadata(meta))

        try:
            # Use upsert to add new or update existing documents
            self._collection.upsert(
                ids=ids,
                embeddings=embeddings,  # type: ignore[arg-type]
                metadatas=normalized_metadatas,  # type: ignore[arg-type]
                documents=documents,
            )
            logger.debug(f"Added/updated {len(ids)} documents in collection")
        except ChromaError as e:
            msg = f"Failed to add documents to ChromaDB: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

    def _normalize_single_metadata(
        self, meta: Dict[str, Any]
    ) -> Dict[str, Union[str, int, float, bool]]:
        """Normalize single metadata dict for ChromaDB compatibility.

        ChromaDB requires metadata values to be str, int, float, or bool.
        This method converts lists to comma-separated strings and nested dicts to
        flattened keys.

        Args:
            meta: Metadata dictionary.

        Returns:
            Normalized metadata dictionary.
        """
        norm_meta: Dict[str, Union[str, int, float, bool]] = {}
        for key, value in meta.items():
            if isinstance(value, (str, int, float, bool)):
                norm_meta[key] = value
            elif isinstance(value, list):
                # Convert lists to comma-separated strings
                norm_meta[key] = ",".join(str(v) for v in value)
            elif value is None:
                # Skip None values or convert to empty string
                norm_meta[key] = ""
            else:
                # Convert anything else to string
                norm_meta[key] = str(value)
        return norm_meta

    async def search(
        self,
        query_embedding: List[float],
        filter_dict: Optional[Dict[str, Any]] = None,
        top_k: int = 10,
        include: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Perform vector similarity search with optional metadata filtering.

        Supports ChromaDB's Where filter syntax for metadata filtering.
        See: https://docs.trychroma.com/reference/where-queries

        Args:
            query_embedding: The query vector to search for.
            filter_dict: Optional metadata filters (e.g., {"tags": {"$contains": "research"}}).
            top_k: Number of results to return (default: 10).
            include: Fields to include in results ("metadatas", "documents", "distances").

        Returns:
            List of result dictionaries with requested fields.

        Raises:
            ValueError: If top_k is invalid or query_embedding has wrong dimensions.
            RuntimeError: If search operation fails or store is not initialized.
        """
        if self._collection is None:
            raise RuntimeError(
                "ChromaDBStore not initialized. Call initialize() first."
            )

        # Validate query embedding
        if len(query_embedding) != self._embedding_dim:
            raise ValueError(
                f"Query embedding dimension {len(query_embedding)} "
                f"does not match expected {self._embedding_dim}"
            )

        # Validate top_k
        if not isinstance(top_k, int) or top_k <= 0:
            raise ValueError(f"top_k must be a positive integer, got {top_k}")

        # Parse filter_dict to ChromaDB where clause
        where_clause = self._parse_filter(filter_dict) if filter_dict else None

        # Default include parameters
        if include is None:
            include = ["metadatas", "documents", "distances"]

        # ChromaDB uses specific include format
        include_params = set(include)
        chroma_include: List[str] = []
        if "metadatas" in include_params:
            chroma_include.append("metadatas")
        if "documents" in include_params:
            chroma_include.append("documents")
        if "distances" in include_params:
            chroma_include.append("distances")

        try:
            results = self._collection.query(
                query_embeddings=[query_embedding],  # type: ignore[arg-type]
                n_results=top_k,
                where=where_clause,
                include=chroma_include,  # type: ignore[arg-type]
            )

            return self._format_results(results, include_params)

        except ChromaError as e:
            msg = f"Search operation failed: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

    def _parse_filter(self, filter_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Parse filter dictionary to ChromaDB where clause format.

        Supports ChromaDB operators: $eq, $ne, $gt, $gte, $lt, $lte,
        $in, $nin, $and, $or.

        Also supports Grimoire-specific aliases for convenience:
        - $contains -> converted to $in for list fields

        Args:
            filter_dict: Filter dictionary with operators.

        Returns:
            Parsed where clause for ChromaDB.

        Raises:
            ValueError: If filter_dict contains unsupported operators.
        """
        VALID_OPERATORS = {
            "$eq",
            "$ne",
            "$gt",
            "$gte",
            "$lt",
            "$lte",
            "$in",
            "$nin",
            "$and",
            "$or",
        }
        GRIFFIN_OPERATORS = {"$contains"}

        def parse_value(key: str, value: Any) -> Any:
            """Parse a filter value, handling operators and nested structures."""
            if isinstance(value, dict):
                parsed = {}
                for op, val in value.items():
                    if op in GRIFFIN_OPERATORS:
                        if op == "$contains":
                            # Convert $contains to $in for comma-separated values
                            # This is an approximation; actual list matching
                            # requires special handling
                            parsed["$in"] = [val]
                    elif op not in VALID_OPERATORS:
                        raise ValueError(
                            f"Unsupported operator '{op}' in filter for key '{key}'. "
                            f"Valid operators: {', '.join(sorted(VALID_OPERATORS | GRIFFIN_OPERATORS))}"
                        )
                    else:
                        parsed[op] = val
                return parsed
            return {"$eq": value}

        where_clause: Dict[str, Any] = {}

        for key, value in filter_dict.items():
            if key in ("$and", "$or"):
                # Logical operators get special handling
                if not isinstance(value, list):
                    raise ValueError(f"{key} operator requires a list of conditions")
                where_clause[key] = [self._parse_filter(v) for v in value]
            else:
                where_clause[key] = parse_value(key, value)

        return where_clause

    def _format_results(
        self,
        results: Mapping[str, Any],
        include_params: set[str],
    ) -> List[Dict[str, Any]]:
        """Format ChromaDB query results to standardized format.

        Args:
            results: Raw ChromaDB query results.
            include_params: Set of fields to include.

        Returns:
            List of formatted result dictionaries.
        """
        formatted_results: List[Dict[str, Any]] = []

        # ChromaDB returns results as nested lists: result[0] is first query
        ids = results.get("ids", [[]])[0] if results.get("ids") else []
        documents = (
            results.get("documents", [[]])[0] if results.get("documents") else []
        )
        metadatas = (
            results.get("metadatas", [[]])[0] if results.get("metadatas") else []
        )
        distances = (
            results.get("distances", [[]])[0] if results.get("distances") else []
        )

        for i, doc_id in enumerate(ids):
            result: Dict[str, Any] = {"id": doc_id}

            if "documents" in include_params and i < len(documents):
                result["document"] = documents[i]

            if "metadatas" in include_params and i < len(metadatas):
                metadata = metadatas[i] if metadatas[i] is not None else {}
                # Restore list values from comma-separated strings for tags
                if isinstance(metadata, dict):
                    # Remove the _empty marker if present
                    if "_empty" in metadata:
                        del metadata["_empty"]
                    for mkey, mval in metadata.items():
                        if isinstance(mval, str) and "," in mval:
                            # Potential comma-separated list
                            metadata[mkey] = [v.strip() for v in mval.split(",")]
                result["metadata"] = metadata

            if "distances" in include_params and i < len(distances):
                result["distance"] = distances[i]

            formatted_results.append(result)

        return formatted_results

    async def delete(self, ids: List[str]) -> None:
        """Delete documents by ID.

        Args:
            ids: Document IDs to delete.

        Raises:
            RuntimeError: If deletion operation fails or store is not initialized.
            ValueError: If ids is empty.
        """
        if self._collection is None:
            raise RuntimeError(
                "ChromaDBStore not initialized. Call initialize() first."
            )

        if not ids:
            raise ValueError("Cannot delete empty list of IDs")

        try:
            self._collection.delete(ids=ids)
            logger.debug(f"Deleted {len(ids)} documents from collection")
        except NotFoundError as e:
            msg = f"Collection or document not found: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e
        except ChromaError as e:
            msg = f"Failed to delete documents: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

    async def get(self, ids: List[str]) -> List[Dict[str, Any]]:
        """Retrieve documents by ID.

        Args:
            ids: Document IDs to retrieve.

        Returns:
            List of document dictionaries with embeddings and metadata.

        Raises:
            RuntimeError: If retrieval operation fails or store is not initialized.
            ValueError: If ids is empty.
        """
        if self._collection is None:
            raise RuntimeError(
                "ChromaDBStore not initialized. Call initialize() first."
            )

        if not ids:
            raise ValueError("Cannot get empty list of IDs")

        try:
            results = self._collection.get(
                ids=ids,
                include=["metadatas", "documents", "embeddings"],
            )

            # Handle None values from ChromaDB API
            result_ids: List[str] = cast(List[str], results.get("ids") or [])
            raw_docs = results.get("documents")
            raw_meta = results.get("metadatas")
            raw_emb = results.get("embeddings")

            documents: Sequence[Optional[str]] = (
                cast(List[Any], raw_docs) if raw_docs is not None else []
            )
            metadatas: Sequence[Optional[Dict[str, Any]]] = (
                cast(List[Any], raw_meta) if raw_meta is not None else []
            )
            embeddings: Sequence[Any] = (
                cast(List[Any], raw_emb) if raw_emb is not None else []
            )

            formatted: List[Dict[str, Any]] = []

            for i, doc_id in enumerate(result_ids):
                record: Dict[str, Any] = {"id": doc_id}

                if i < len(documents) and documents[i] is not None:
                    record["document"] = documents[i]

                if i < len(metadatas) and metadatas[i] is not None:
                    metadata = metadatas[i]
                    # Restore list values from comma-separated strings
                    if isinstance(metadata, dict):
                        # Remove the _empty marker if present
                        if "_empty" in metadata:
                            del metadata["_empty"]
                        for mkey, mval in metadata.items():
                            if isinstance(mval, str) and "," in mval:
                                metadata[mkey] = [v.strip() for v in mval.split(",")]
                    record["metadata"] = metadata

                if i < len(embeddings) and embeddings[i] is not None:
                    # ChromaDB returns embeddings as numpy arrays or sequences
                    record["embedding"] = list(embeddings[i])

                formatted.append(record)

            return formatted

        except ChromaError as e:
            msg = f"Failed to retrieve documents: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

    async def count(self) -> int:
        """Get total document count in the collection.

        Returns:
            Number of documents stored.

        Raises:
            RuntimeError: If count operation fails or store is not initialized.
        """
        if self._collection is None:
            raise RuntimeError(
                "ChromaDBStore not initialized. Call initialize() first."
            )

        try:
            return self._collection.count()
        except ChromaError as e:
            msg = f"Failed to get document count: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e

    @property
    def is_initialized(self) -> bool:
        """Check if the store has been initialized.

        Returns:
            True if initialize() has been called successfully.
        """
        return self._collection is not None

    async def reset(self) -> None:
        """Reset the collection by deleting all documents.

        Useful for testing and cleanup operations.

        Raises:
            RuntimeError: If reset operation fails.
        """
        if self._collection is None:
            raise RuntimeError(
                "ChromaDBStore not initialized. Call initialize() first."
            )

        try:
            # Get all IDs and delete them
            all_ids = self._collection.get(include=[]).get("ids", [])
            if all_ids:
                self._collection.delete(ids=all_ids)
            logger.info(
                f"Reset collection '{self.collection_name}' - removed all documents"
            )
        except ChromaError as e:
            msg = f"Failed to reset collection: {e}"
            logger.error(msg)
            raise RuntimeError(msg) from e
