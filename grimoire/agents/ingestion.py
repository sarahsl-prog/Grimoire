"""Ingestion Agent for document processing pipeline.

Orchestrates the full document ingestion flow:
    File Discovery -> Dedup Check -> Parse -> Chunk -> Embed -> Store Vectors
    -> Auto-Tag -> Log Processing

The agent coordinates all core services to process documents from
any storage backend into the vector store and metadata database.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from grimoire.core.chunker import Chunk, ChunkConfig, ChunkingStrategy, Chunker
from grimoire.core.chunker.markdown import MarkdownHeaderTextSplitter
from grimoire.core.chunker.recursive import RecursiveCharacterTextSplitter, RecursiveChunkConfig
from grimoire.core.chunker.semantic import SemanticChunker
from grimoire.core.dedup import DedupResult, DeduplicationAction, Deduplicator
from grimoire.core.embedder import Embedder
from grimoire.core.parser import DocumentParser, ParsedDocument
from grimoire.core.tagger import Tagger
from grimoire.db.models import (
    ActionType,
    Category,
    Chunk as ChunkModel,
    Document,
    FileType,
    ProcessingLog,
    ProcessingStatus,
    StatusType,
    StorageBackend,
)
from grimoire.vectorstore.base import VectorStore


# =============================================================================
# Data Models
# =============================================================================


class IngestionResult(BaseModel):
    """Result of processing a single document.

    Attributes:
        file_path: Path to the processed file.
        document_id: UUID of the created/updated document record.
        status: Processing outcome (completed, skipped, failed).
        chunks_created: Number of chunks generated.
        vectors_stored: Number of vectors added to the vector store.
        tags_applied: Number of auto-tags applied.
        error_message: Error details if processing failed.
        duration_ms: Total processing time in milliseconds.
    """

    model_config = ConfigDict(extra="allow")

    file_path: str
    document_id: Optional[str] = None
    status: str = "completed"
    chunks_created: int = 0
    vectors_stored: int = 0
    tags_applied: int = 0
    error_message: Optional[str] = None
    duration_ms: int = 0


class BatchIngestionResult(BaseModel):
    """Result of processing multiple documents.

    Attributes:
        total: Total number of files processed.
        succeeded: Number of successfully processed files.
        skipped: Number of skipped files (duplicates).
        failed: Number of failed files.
        results: Per-file results.
        duration_ms: Total batch processing time.
    """

    total: int = 0
    succeeded: int = 0
    skipped: int = 0
    failed: int = 0
    results: List[IngestionResult] = Field(default_factory=list)
    duration_ms: int = 0


# =============================================================================
# File Type Detection
# =============================================================================

# Map file extensions to FileType enum values
_EXTENSION_TO_FILE_TYPE: dict[str, FileType] = {
    ".pdf": FileType.PDF,
    ".docx": FileType.DOCX,
    ".doc": FileType.DOCX,
    ".pptx": FileType.PPTX,
    ".ppt": FileType.PPTX,
    ".xlsx": FileType.XLSX,
    ".xls": FileType.XLSX,
    ".html": FileType.HTML,
    ".htm": FileType.HTML,
    ".md": FileType.MD,
    ".txt": FileType.TXT,
    ".png": FileType.IMAGE,
    ".jpg": FileType.IMAGE,
    ".jpeg": FileType.IMAGE,
    ".tiff": FileType.IMAGE,
    ".tif": FileType.IMAGE,
    ".gif": FileType.IMAGE,
    ".bmp": FileType.IMAGE,
    ".webp": FileType.IMAGE,
}


def detect_file_type(file_path: str | Path) -> FileType:
    """Detect file type from extension.

    Args:
        file_path: Path to the file.

    Returns:
        Detected FileType enum value.
    """
    suffix = Path(file_path).suffix.lower()
    return _EXTENSION_TO_FILE_TYPE.get(suffix, FileType.OTHER)


def _select_chunking_strategy(file_path: str | Path) -> ChunkingStrategy:
    """Select the best chunking strategy based on file type.

    Args:
        file_path: Path to the file.

    Returns:
        Appropriate ChunkingStrategy.
    """
    suffix = Path(file_path).suffix.lower()
    if suffix == ".md":
        return ChunkingStrategy.MARKDOWN
    return ChunkingStrategy.RECURSIVE


# =============================================================================
# Ingestion Agent
# =============================================================================


class IngestionAgent:
    """Orchestrates the document ingestion pipeline.

    Coordinates parsing, chunking, embedding, vector storage,
    auto-tagging, and metadata persistence for document ingestion.

    Args:
        parser: Document parser instance.
        embedder: Embedding service instance.
        vector_store: Vector store instance.
        tagger: Optional auto-tagger instance.
        chunk_config: Chunking configuration.
        storage_backend: Default storage backend for new documents.
        embedding_model: Name of the embedding model (for metadata).

    Example:
        ```python
        agent = IngestionAgent(
            parser=DocumentParser(),
            embedder=Embedder(),
            vector_store=chromadb_store,
        )
        async with get_db_context() as db:
            result = await agent.ingest_file(db, "/path/to/doc.pdf")
        ```
    """

    def __init__(
        self,
        parser: DocumentParser,
        embedder: Embedder,
        vector_store: VectorStore,
        tagger: Optional[Tagger] = None,
        chunk_config: Optional[ChunkConfig] = None,
        storage_backend: StorageBackend = StorageBackend.LOCAL,
        embedding_model: str = "sentence-transformers/all-mpnet-base-v2",
    ) -> None:
        self._parser = parser
        self._embedder = embedder
        self._vector_store = vector_store
        self._tagger = tagger
        self._chunk_config = chunk_config or ChunkConfig()
        self._storage_backend = storage_backend
        self._embedding_model = embedding_model
        self._deduplicator = Deduplicator()

        logger.debug("IngestionAgent initialized")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def ingest_file(
        self,
        db: AsyncSession,
        file_path: str | Path,
        *,
        storage_backend: Optional[StorageBackend] = None,
        auto_tag: bool = True,
        categories: Optional[List[Category]] = None,
    ) -> IngestionResult:
        """Ingest a single file through the full pipeline.

        Args:
            db: Database session.
            file_path: Path to the file to ingest.
            storage_backend: Override default storage backend.
            auto_tag: Whether to auto-tag the document.
            categories: Categories for auto-tagging (fetched from DB if None).

        Returns:
            IngestionResult with processing details.
        """
        start_time = time.monotonic()
        file_path = Path(file_path)
        backend = storage_backend or self._storage_backend

        logger.info(f"Ingesting file: {file_path}")

        # Ensure vector store is initialized (lazy init)
        if hasattr(self._vector_store, 'is_initialized') and not self._vector_store.is_initialized:
            embedding_dim = self._embedder.embedding_dim
            await self._vector_store.initialize(
                collection_name=getattr(self._vector_store, 'collection_name', 'documents'),
                embedding_dim=embedding_dim,
            )
            logger.debug("Vector store initialized")

        try:
            # Step 1: Detect file type
            file_type = detect_file_type(file_path)
            logger.debug(f"Detected file type: {file_type.value}")

            # Step 2: Deduplication check
            dedup_result = await self._check_dedup(db, file_path)
            if dedup_result.action == DeduplicationAction.SKIP:
                logger.info(f"Skipping duplicate: {file_path}")
                return IngestionResult(
                    file_path=str(file_path),
                    document_id=(
                        dedup_result.existing_document.id
                        if dedup_result.existing_document
                        else None
                    ),
                    status="skipped",
                    duration_ms=self._elapsed_ms(start_time),
                )

            # Step 3: Parse the document
            parsed = await self._parser.parse(file_path)
            if parsed.status == "failed" or not parsed.text.strip():
                error_msg = parsed.error_message or "No text extracted"
                logger.warning(f"Parse failed for {file_path}: {error_msg}")
                doc = await self._create_document_record(
                    db,
                    file_path,
                    file_type,
                    backend,
                    dedup_result.file_hash,
                    parsed,
                    ProcessingStatus.FAILED,
                    error_msg,
                )
                await self._log_processing(
                    db, doc.id, ActionType.EXTRACTED, StatusType.FAILED,
                    {"error": error_msg},
                )
                return IngestionResult(
                    file_path=str(file_path),
                    document_id=doc.id,
                    status="failed",
                    error_message=error_msg,
                    duration_ms=self._elapsed_ms(start_time),
                )

            await self._log_extraction(db, file_path, parsed, start_time)

            # Step 4: Create or update document record
            if dedup_result.action == DeduplicationAction.UPDATE:
                doc = dedup_result.existing_document
                await self._update_document_record(
                    db, doc, dedup_result.file_hash, parsed,
                )
            else:
                doc = await self._create_document_record(
                    db, file_path, file_type, backend,
                    dedup_result.file_hash, parsed,
                    ProcessingStatus.PROCESSING,
                )

            # Step 5: Chunk the document
            chunks = await self._chunk_document(parsed.text, str(file_path), doc.id)
            await self._log_processing(
                db, doc.id, ActionType.CHUNKED, StatusType.SUCCESS,
                {"chunk_count": len(chunks)},
            )

            # Step 6: Store chunks in DB
            chunk_models = await self._store_chunks_in_db(db, doc.id, chunks)

            # Step 7: Embed and store vectors
            vectors_stored = await self._embed_and_store(
                db, doc.id, chunks, chunk_models,
            )

            # Step 8: Auto-tag (optional)
            tags_applied = 0
            if auto_tag and self._tagger:
                if categories is None:
                    categories = await self._fetch_categories(db)
                tags_applied = await self._auto_tag(
                    db, doc, parsed.text, categories,
                )

            # Step 9: Mark as completed
            doc.processing_status = ProcessingStatus.COMPLETED
            doc.processed_at = datetime.utcnow()
            doc.error_message = None
            await db.flush()

            duration = self._elapsed_ms(start_time)
            logger.info(
                f"Ingestion complete: {file_path} "
                f"(chunks={len(chunks)}, vectors={vectors_stored}, "
                f"tags={tags_applied}, {duration}ms)"
            )

            return IngestionResult(
                file_path=str(file_path),
                document_id=doc.id,
                status="completed",
                chunks_created=len(chunks),
                vectors_stored=vectors_stored,
                tags_applied=tags_applied,
                duration_ms=duration,
            )

        except Exception as e:
            logger.error(f"Ingestion failed for {file_path}: {e}")
            return IngestionResult(
                file_path=str(file_path),
                status="failed",
                error_message=str(e),
                duration_ms=self._elapsed_ms(start_time),
            )

    async def ingest_directory(
        self,
        db: AsyncSession,
        directory: str | Path,
        *,
        recursive: bool = True,
        storage_backend: Optional[StorageBackend] = None,
        auto_tag: bool = True,
    ) -> BatchIngestionResult:
        """Ingest all supported files in a directory.

        Args:
            db: Database session.
            directory: Directory path to scan.
            recursive: Whether to scan subdirectories.
            storage_backend: Override default storage backend.
            auto_tag: Whether to auto-tag documents.

        Returns:
            BatchIngestionResult with per-file details.
        """
        start_time = time.monotonic()
        directory = Path(directory)

        if not directory.is_dir():
            raise ValueError(f"Not a directory: {directory}")

        # Discover files
        files = self._discover_files(directory, recursive)
        logger.info(f"Discovered {len(files)} files in {directory}")

        # Fetch categories once for the batch
        categories = None
        if auto_tag and self._tagger:
            categories = await self._fetch_categories(db)

        batch_result = BatchIngestionResult(total=len(files))

        for file_path in files:
            result = await self.ingest_file(
                db, file_path,
                storage_backend=storage_backend,
                auto_tag=auto_tag,
                categories=categories,
            )
            batch_result.results.append(result)

            if result.status == "completed":
                batch_result.succeeded += 1
            elif result.status == "skipped":
                batch_result.skipped += 1
            else:
                batch_result.failed += 1

        batch_result.duration_ms = self._elapsed_ms(start_time)
        logger.info(
            f"Batch ingestion complete: {batch_result.succeeded} succeeded, "
            f"{batch_result.skipped} skipped, {batch_result.failed} failed "
            f"({batch_result.duration_ms}ms)"
        )
        return batch_result

    # -------------------------------------------------------------------------
    # Pipeline Steps (private)
    # -------------------------------------------------------------------------

    async def _check_dedup(
        self, db: AsyncSession, file_path: Path,
    ) -> DedupResult:
        """Check for duplicate documents.

        Args:
            db: Database session.
            file_path: Path to the file.

        Returns:
            DedupResult indicating what action to take.
        """
        from grimoire.core.dedup import compute_file_hash

        file_hash = compute_file_hash(file_path)

        # Look up existing document by hash
        stmt = select(Document).where(Document.file_hash == file_hash)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is not None:
            dedup_result = await self._deduplicator.check_file(
                file_path, existing_doc=existing,
            )
        else:
            dedup_result = DedupResult(
                action=DeduplicationAction.NEW,
                file_hash=file_hash,
            )

        return dedup_result

    async def _chunk_document(
        self, text: str, file_path: str, doc_id: str,
    ) -> List[Chunk]:
        """Chunk document text using the appropriate strategy.

        Args:
            text: Extracted document text.
            file_path: File path for strategy selection.
            doc_id: Document ID for chunk metadata.

        Returns:
            List of Chunk objects.
        """
        strategy = _select_chunking_strategy(file_path)
        chunker = self._create_chunker(strategy)

        chunks = await chunker.chunk(text, doc_id=doc_id)
        logger.debug(f"Chunked into {len(chunks)} chunks (strategy={strategy.value})")
        return chunks

    def _create_chunker(self, strategy: ChunkingStrategy) -> Chunker:
        """Create a chunker for the given strategy.

        Args:
            strategy: Chunking strategy to use.

        Returns:
            Configured Chunker instance.
        """
        size = self._chunk_config.chunk_size
        overlap = self._chunk_config.chunk_overlap
        if strategy == ChunkingStrategy.MARKDOWN:
            from grimoire.core.chunker.markdown import MarkdownChunkConfig
            return MarkdownHeaderTextSplitter(
                MarkdownChunkConfig(chunk_size=size, chunk_overlap=overlap)
            )
        elif strategy == ChunkingStrategy.SEMANTIC:
            from grimoire.core.chunker.semantic import SemanticChunkConfig
            return SemanticChunker(
                SemanticChunkConfig(chunk_size=size, chunk_overlap=overlap)
            )
        else:
            return RecursiveCharacterTextSplitter(
                RecursiveChunkConfig(chunk_size=size, chunk_overlap=overlap)
            )

    async def _store_chunks_in_db(
        self, db: AsyncSession, doc_id: str, chunks: List[Chunk],
    ) -> List[ChunkModel]:
        """Persist chunks to the database.

        Args:
            db: Database session.
            doc_id: Parent document ID.
            chunks: Chunks to store.

        Returns:
            List of created ChunkModel instances.
        """
        chunk_models: List[ChunkModel] = []
        for chunk in chunks:
            chunk_model = ChunkModel(
                id=chunk.metadata.get("chunk_id", str(uuid4())),
                document_id=doc_id,
                chunk_index=chunk.index,
                content=chunk.content,
                token_count=chunk.token_count,
                embedding_model=self._embedding_model,
            )
            db.add(chunk_model)
            chunk_models.append(chunk_model)

        # Insert chunks first without continuity links to avoid FK violations
        await db.flush()

        # Now set prev/next links since all chunks exist in the DB
        for chunk, chunk_model in zip(chunks, chunk_models):
            chunk_model.prev_chunk_id = chunk.prev_chunk_id
            chunk_model.next_chunk_id = chunk.next_chunk_id

        await db.flush()
        logger.debug(f"Stored {len(chunk_models)} chunks in database")
        return chunk_models

    async def _embed_and_store(
        self,
        db: AsyncSession,
        doc_id: str,
        chunks: List[Chunk],
        chunk_models: List[ChunkModel],
    ) -> int:
        """Embed chunks and store vectors.

        Args:
            db: Database session.
            doc_id: Parent document ID.
            chunks: Chunk data objects.
            chunk_models: Corresponding DB models.

        Returns:
            Number of vectors stored.
        """
        if not chunks:
            return 0

        texts = [c.content for c in chunks]
        embeddings = await self._embedder.embed(texts)

        ids = [cm.id for cm in chunk_models]
        metadatas = [
            {
                "document_id": doc_id,
                "chunk_index": c.index,
                "token_count": c.token_count,
            }
            for c in chunks
        ]

        await self._vector_store.add_documents(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=texts,
        )

        # Update vector_id references in chunk models
        for cm, vid in zip(chunk_models, ids):
            cm.vector_id = vid
        await db.flush()

        logger.debug(f"Stored {len(embeddings)} vectors")
        return len(embeddings)

    async def _auto_tag(
        self,
        db: AsyncSession,
        doc: Document,
        text: str,
        categories: Optional[List[Category]],
    ) -> int:
        """Auto-tag a document using the LLM tagger.

        Args:
            db: Database session.
            doc: Document to tag.
            text: Document text for analysis.
            categories: Available categories.

        Returns:
            Number of tags applied.
        """
        if not self._tagger or not categories:
            return 0

        try:
            result = await self._tagger.tag_document(
                db_session=db,
                document=doc,
                categories=categories,
                sample=text,
                auto_apply=True,
            )
            tags_applied = len(result.applied_tags) if result.applied_tags else 0
            if tags_applied > 0:
                await self._log_processing(
                    db, doc.id, ActionType.TAGGED, StatusType.SUCCESS,
                    {"tags_applied": tags_applied, "model": result.model_used},
                )
            return tags_applied
        except Exception as e:
            logger.warning(f"Auto-tagging failed for document {doc.id}: {e}")
            await self._log_processing(
                db, doc.id, ActionType.TAGGED, StatusType.FAILED,
                {"error": str(e)},
            )
            return 0

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    async def _create_document_record(
        self,
        db: AsyncSession,
        file_path: Path,
        file_type: FileType,
        backend: StorageBackend,
        file_hash: str,
        parsed: ParsedDocument,
        status: ProcessingStatus = ProcessingStatus.PROCESSING,
        error_message: Optional[str] = None,
    ) -> Document:
        """Create a new document record in the database.

        Args:
            db: Database session.
            file_path: Path to the file.
            file_type: Detected file type.
            backend: Storage backend.
            file_hash: SHA-256 hash.
            parsed: Parsed document result.
            status: Initial processing status.
            error_message: Optional error message.

        Returns:
            Created Document instance.
        """
        doc = Document(
            source_path=str(file_path),
            storage_backend=backend,
            file_type=file_type,
            file_hash=file_hash,
            title=parsed.metadata.title or file_path.stem,
            size_bytes=parsed.metadata.file_size or file_path.stat().st_size,
            processing_status=status,
            error_message=error_message,
        )
        db.add(doc)
        await db.flush()
        logger.debug(f"Created document record: {doc.id}")
        return doc

    async def _update_document_record(
        self,
        db: AsyncSession,
        doc: Document,
        file_hash: str,
        parsed: ParsedDocument,
    ) -> None:
        """Update an existing document record.

        Removes old chunks and vectors before re-processing.

        Args:
            db: Database session.
            doc: Existing document to update.
            file_hash: New file hash.
            parsed: New parsed result.
        """
        # Delete old vectors from vector store
        old_chunk_ids = [c.id for c in doc.chunks]
        if old_chunk_ids:
            try:
                await self._vector_store.delete(old_chunk_ids)
            except Exception as e:
                logger.warning(f"Failed to delete old vectors: {e}")

        # Delete old chunks from DB (cascade handles this)
        for chunk in list(doc.chunks):
            await db.delete(chunk)

        doc.file_hash = file_hash
        doc.title = parsed.metadata.title or doc.title
        doc.processing_status = ProcessingStatus.PROCESSING
        doc.version += 1
        doc.updated_at = datetime.utcnow()
        await db.flush()
        logger.debug(f"Updated document record: {doc.id} (v{doc.version})")

    async def _log_processing(
        self,
        db: AsyncSession,
        document_id: str,
        action: ActionType,
        status: StatusType,
        details: Optional[dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        """Log a processing step to the audit trail.

        Args:
            db: Database session.
            document_id: Document being processed.
            action: Type of processing action.
            status: Outcome of the action.
            details: Additional details as JSON.
            duration_ms: Processing duration.
        """
        log_entry = ProcessingLog(
            document_id=document_id,
            action=action,
            status=status,
            details=details,
            duration_ms=duration_ms,
        )
        db.add(log_entry)
        await db.flush()

    async def _log_extraction(
        self,
        db: AsyncSession,
        file_path: Path,
        parsed: ParsedDocument,
        start_time: float,
    ) -> None:
        """Log successful extraction (before document record may exist)."""
        # This is called before the document record exists for new files,
        # so we skip logging here and log after the document is created.
        pass

    async def _fetch_categories(self, db: AsyncSession) -> List[Category]:
        """Fetch all categories from the database.

        Args:
            db: Database session.

        Returns:
            List of Category objects.
        """
        stmt = select(Category)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    def _discover_files(
        self, directory: Path, recursive: bool,
    ) -> List[Path]:
        """Discover supported files in a directory.

        Args:
            directory: Directory to scan.
            recursive: Whether to scan subdirectories.

        Returns:
            List of file paths with supported extensions.
        """
        supported = DocumentParser.SUPPORTED_EXTENSIONS | {".md", ".txt"}
        files: List[Path] = []

        if recursive:
            for path in directory.rglob("*"):
                if path.is_file() and path.suffix.lower() in supported:
                    files.append(path)
        else:
            for path in directory.iterdir():
                if path.is_file() and path.suffix.lower() in supported:
                    files.append(path)

        return sorted(files)

    @staticmethod
    def _elapsed_ms(start_time: float) -> int:
        """Calculate elapsed time in milliseconds."""
        return int((time.monotonic() - start_time) * 1000)
