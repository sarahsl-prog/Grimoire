"""Deduplication logic for document ingestion.

Provides SHA-256 hash-based deduplication with configurable conflict
resolution strategies. Handles version conflicts and stores resolution
decisions in the processing log.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, EnumMeta
from pathlib import Path
from typing import Any, Optional, Protocol

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from grimoire.db.models import ActionType, Document, ProcessingLog, StatusType

# ============================================================================
# Pydantic Models
# ============================================================================


class DedupStrategy(str, Enum):
    """Strategy for resolving version conflicts during deduplication.

    Attributes:
        AUTO: Automatically update if file is newer (default)
        SKIP: Keep existing document, skip new version
        DUPLICATE: Store both versions with different version numbers
        MANUAL: Flag for manual review (logs conflict, requires human)
    """

    AUTO = "auto"
    SKIP = "skip"
    DUPLICATE = "duplicate"
    MANUAL = "manual"


class DeduplicationAction(str, Enum):
    """Action determined by the deduplication check."""

    NEW = "new"  # New document, no existing record
    SKIP = "skip"  # Skip processing (duplicate or strategy)
    UPDATE = "update"  # Update existing document
    CONFLICT = "conflict"  # Version conflict requires resolution


class DedupResult(BaseModel):
    """Result of a deduplication check.

    Attributes:
        action: The determined action (new, skip, update, conflict)
        file_hash: SHA-256 hash of the file content
        existing_document: The existing document if found (None for new files)
        conflict: True if a version conflict was detected
        strategy: The strategy used to resolve the conflict
        resolution: Human-readable description of the resolution
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    action: DeduplicationAction
    file_hash: str
    existing_document: Optional[Any] = Field(default=None)
    conflict: bool = Field(default=False)
    strategy: DedupStrategy = Field(default=DedupStrategy.AUTO)
    resolution: str = Field(default="")


class ConflictDetails(BaseModel):
    """Details about a version conflict for logging.

    Attributes:
        existing_hash: Hash of the existing document
        new_hash: Hash of the new file
        existing_mtime: Last modified time of existing document
        new_mtime: Last modified time of new file
        existing_version: Version number of existing document
        strategy_used: Strategy used to resolve the conflict
    """

    existing_hash: str
    new_hash: str
    existing_mtime: Optional[datetime] = None
    new_mtime: Optional[datetime] = None
    existing_version: int
    strategy_used: DedupStrategy


# ============================================================================
# Hash Computation
# ============================================================================


CHUNK_SIZE = 8192  # 8KB chunks for memory efficiency


def compute_file_hash(file_path: Path | str) -> str:
    """Compute SHA-256 hash of file content.

    Reads file in chunks to handle large files efficiently without
    loading entire content into memory.

    Args:
        file_path: Path to the file to hash

    Returns:
        Hexadecimal string of the SHA-256 hash

    Raises:
        FileNotFoundError: If file does not exist
        PermissionError: If file cannot be read
        OSError: For other file system errors

    Example:
        >>> hash_value = compute_file_hash("/path/to/document.pdf")
        >>> print(hash_value)
        'a3f7c2d8...'
    """
    path = Path(file_path)
    hasher = hashlib.sha256()

    logger.debug(f"Computing SHA-256 hash for {path}")

    try:
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                hasher.update(chunk)
    except FileNotFoundError:
        logger.error(f"File not found: {path}")
        raise
    except PermissionError:
        logger.error(f"Permission denied reading file: {path}")
        raise
    except OSError as e:
        logger.error(f"Error reading file {path}: {e}")
        raise

    hash_value = hasher.hexdigest()
    logger.debug(f"Computed hash for {path}: {hash_value[:16]}...")

    return hash_value


def compute_bytes_hash(content: bytes) -> str:
    """Compute SHA-256 hash of byte content.

    Args:
        content: Raw bytes to hash

    Returns:
        Hexadecimal string of the SHA-256 hash

    Example:
        >>> hash_value = compute_bytes_hash(b"Hello world")
        >>> print(hash_value)
        '64ec88ca...'
    """
    return hashlib.sha256(content).hexdigest()


# ============================================================================
# Deduplication Logic
# ============================================================================


class Deduplicator:
    """Handles document deduplication with conflict resolution.

    The Deduplicator computes file hashes and compares them against
    existing documents in the database. It handles version conflicts
    using configurable strategies and logs all decisions.

    Attributes:
        default_strategy: The default conflict resolution strategy to use
    """

    def __init__(self, default_strategy: DedupStrategy = DedupStrategy.AUTO) -> None:
        """Initialize the deduplicator with a default strategy.

        Args:
            default_strategy: Default strategy for conflict resolution
        """
        self.default_strategy = default_strategy
        logger.info(f"Initialized Deduplicator with strategy: {default_strategy.value}")

    async def check_file(
        self,
        file_path: Path | str,
        existing_doc: Optional[Document] = None,
        file_mtime: Optional[datetime] = None,
        strategy: Optional[DedupStrategy] = None,
    ) -> DedupResult:
        """Check if a file needs to be processed based on deduplication logic.

        Compares file hash against existing document (if any) and determines
        the appropriate action based on the configured strategy.

        Args:
            file_path: Path to the file to check
            existing_doc: Existing document record from database (if any)
            file_mtime: File modification time (for version comparison)
            strategy: Override the default conflict resolution strategy

        Returns:
            DedupResult with action, hash, and resolution details

        Raises:
            FileNotFoundError: If file does not exist
            PermissionError: If file cannot be read

        Example:
            >>> deduplicator = Deduplicator()
            >>> result = await deduplicator.check_file(
            ...     "/path/to/file.pdf",
            ...     existing_doc=doc,
            ...     file_mtime=datetime.now()
            ... )
            >>> print(result.action)
            DeduplicationAction.SKIP
        """
        # Compute file hash
        file_hash = compute_file_hash(file_path)
        use_strategy = strategy or self.default_strategy

        # No existing document - this is a new file
        if existing_doc is None:
            logger.debug(f"New file detected: {file_path}")
            return DedupResult(
                action=DeduplicationAction.NEW,
                file_hash=file_hash,
                existing_document=None,
                conflict=False,
                strategy=use_strategy,
                resolution="New file, no existing record",
            )

        # Same hash - file is unchanged
        if existing_doc.file_hash == file_hash:
            logger.debug(f"Duplicate file detected (same hash): {file_path}")
            return DedupResult(
                action=DeduplicationAction.SKIP,
                file_hash=file_hash,
                existing_document=existing_doc,
                conflict=False,
                strategy=use_strategy,
                resolution="File unchanged (same SHA-256 hash)",
            )

        # Different hash - file has been modified
        logger.debug(f"Modified file detected: {file_path}")

        # Check for version conflict
        is_version_conflict = self._is_version_conflict(existing_doc, file_mtime)

        if is_version_conflict:
            logger.warning(f"Version conflict detected for {file_path}")
            return await self._resolve_version_conflict(
                existing_doc=existing_doc,
                file_hash=file_hash,
                file_mtime=file_mtime,
                strategy=use_strategy,
            )

        # No conflict - safe to update
        logger.info(f"Updating existing document: {existing_doc.id}")
        return DedupResult(
            action=DeduplicationAction.UPDATE,
            file_hash=file_hash,
            existing_document=existing_doc,
            conflict=False,
            strategy=use_strategy,
            resolution="File modified, newer version detected",
        )

    def _is_version_conflict(
        self,
        existing_doc: Document,
        file_mtime: Optional[datetime],
    ) -> bool:
        """Determine if a version conflict exists.

        A version conflict occurs when the existing document has a
        modification time newer than the file being processed,
        indicating the file might have been reverted or there's
        concurrent modification.

        Args:
            existing_doc: The existing document record
            file_mtime: The file's current modification time

        Returns:
            True if a version conflict is detected
        """
        if file_mtime is None or existing_doc.updated_at is None:
            return False

        # Conflict if existing document is newer than the file
        return existing_doc.updated_at > file_mtime

    async def _resolve_version_conflict(
        self,
        existing_doc: Document,
        file_hash: str,
        file_mtime: Optional[datetime],
        strategy: DedupStrategy,
    ) -> DedupResult:
        """Resolve a version conflict using the specified strategy.

        Args:
            existing_doc: The existing document with newer version
            file_hash: Hash of the new file being processed
            file_mtime: Modification time of the new file
            strategy: Strategy to use for resolution

        Returns:
            DedupResult with the resolution action
        """
        resolution_msg = ""
        action: DeduplicationAction

        if strategy == DedupStrategy.AUTO:
            # Auto: Update with the newer version (which is existing_doc)
            # But wait - existing_doc is newer, so we skip
            resolution_msg = (
                f"Auto strategy: Keeping newer version (v{existing_doc.version})"
            )
            action = DeduplicationAction.SKIP
            logger.info(resolution_msg)

        elif strategy == DedupStrategy.SKIP:
            resolution_msg = (
                f"Skip strategy: Skipping older file, keeping v{existing_doc.version}"
            )
            action = DeduplicationAction.SKIP
            logger.info(resolution_msg)

        elif strategy == DedupStrategy.DUPLICATE:
            resolution_msg = (
                f"Duplicate strategy: Creating new version v{existing_doc.version + 1}"
            )
            action = DeduplicationAction.UPDATE
            logger.info(resolution_msg)

        else:  # strategy == DedupStrategy.MANUAL
            resolution_msg = f"Manual strategy: Flagging for review (existing v{existing_doc.version})"
            action = DeduplicationAction.CONFLICT
            logger.warning(resolution_msg)

        return DedupResult(
            action=action,
            file_hash=file_hash,
            existing_document=existing_doc,
            conflict=True,
            strategy=strategy,
            resolution=resolution_msg,
        )

    def create_conflict_details(
        self,
        existing_doc: Document,
        new_hash: str,
        new_mtime: Optional[datetime],
        strategy: DedupStrategy,
    ) -> ConflictDetails:
        """Create conflict details for logging.

        Args:
            existing_doc: The existing document
            new_hash: Hash of the new file
            new_mtime: Modification time of the new file
            strategy: Strategy used for resolution

        Returns:
            ConflictDetails dataclass with conflict information
        """
        return ConflictDetails(
            existing_hash=existing_doc.file_hash,
            new_hash=new_hash,
            existing_mtime=existing_doc.updated_at,
            new_mtime=new_mtime,
            existing_version=existing_doc.version,
            strategy_used=strategy,
        )

    def create_processing_log(
        self,
        document_id: str,
        dedup_result: DedupResult,
        duration_ms: Optional[int] = None,
    ) -> ProcessingLog:
        """Create a processing log entry for the deduplication check.

        Args:
            document_id: The document ID being processed
            dedup_result: Result from the deduplication check
            duration_ms: Processing duration in milliseconds

        Returns:
            ProcessingLog model ready to be saved to database
        """
        # Map actions to status
        if dedup_result.action == DeduplicationAction.CONFLICT:
            status = StatusType.FAILED
        elif dedup_result.action == DeduplicationAction.SKIP:
            status = StatusType.PARTIAL
        else:
            status = StatusType.SUCCESS

        # Build details dict
        details: dict[str, Any] = {
            "action": dedup_result.action.value,
            "file_hash": dedup_result.file_hash,
            "conflict": dedup_result.conflict,
            "strategy": dedup_result.strategy.value,
            "resolution": dedup_result.resolution,
        }

        if dedup_result.existing_document:
            details["existing_document_id"] = str(dedup_result.existing_document.id)
            details["existing_version"] = dedup_result.existing_document.version

        return ProcessingLog(
            document_id=document_id,
            action=ActionType.DISCOVERED,
            status=status,
            details=details,
            duration_ms=duration_ms,
        )


# ============================================================================
# Async Helper Functions
# ============================================================================


async def check_duplicate(
    file_path: Path | str,
    existing_doc: Optional[Document] = None,
    file_mtime: Optional[datetime] = None,
    strategy: DedupStrategy = DedupStrategy.AUTO,
) -> DedupResult:
    """Convenience function for one-off deduplication checks.

    Args:
        file_path: Path to the file to check
        existing_doc: Existing document from database (if any)
        file_mtime: File modification time
        strategy: Conflict resolution strategy

    Returns:
        DedupResult with deduplication decision

    Example:
        >>> result = await check_duplicate(
        ...     "/path/to/file.pdf",
        ...     existing_doc=db_doc,
        ...     strategy=DedupStrategy.SKIP
        ... )
    """
    deduplicator = Deduplicator(default_strategy=strategy)
    return await deduplicator.check_file(
        file_path=file_path,
        existing_doc=existing_doc,
        file_mtime=file_mtime,
        strategy=strategy,
    )


def get_file_mtime(file_path: Path | str) -> Optional[datetime]:
    """Get file modification time as a datetime object.

    Args:
        file_path: Path to the file

    Returns:
        datetime of last modification, or None if file doesn't exist
    """
    try:
        path = Path(file_path)
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime)
    except (FileNotFoundError, OSError):
        return None
