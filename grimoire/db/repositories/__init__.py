"""Repository pattern implementation for Grimoire.

This module provides repository classes for accessing and manipulating
domain entities through the database.
"""

from typing import TypeVar, Generic, List, Optional, AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload

from grimoire.db.models import Document, Chunk, Category, DocumentTag, WikiPage

# Type variable for generic repository
T = TypeVar("T")


class BaseRepository(Generic[T]):
    """Base repository class with common CRUD operations."""

    def __init__(self, session: AsyncSession):
        """
        Initialize repository with database session.

        Args:
            session: Async database session
        """
        self.session = session

    async def add(self, entity: T) -> T:
        """
        Add an entity to the database.

        Args:
            entity: Entity to add

        Returns:
            Added entity
        """
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def get_by_id(self, entity_id: str) -> Optional[T]:
        """
        Get an entity by ID.

        Args:
            entity_id: Entity ID

        Returns:
            Entity or None if not found
        """
        stmt = select(self.model).where(self.model.id == entity_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> List[T]:
        """
        List all entities.

        Returns:
            List of all entities
        """
        stmt = select(self.model)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def update(self, entity: T) -> T:
        """
        Update an entity.

        Args:
            entity: Entity to update

        Returns:
            Updated entity
        """
        await self.session.merge(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: T) -> None:
        """
        Delete an entity.

        Args:
            entity: Entity to delete
        """
        await self.session.delete(entity)
        await self.session.flush()

    async def delete_by_id(self, entity_id: str) -> bool:
        """
        Delete an entity by ID.

        Args:
            entity_id: Entity ID

        Returns:
            True if entity was deleted, False if not found
        """
        entity = await self.get_by_id(entity_id)
        if entity:
            await self.delete(entity)
            return True
        return False


class DocumentRepository(BaseRepository[Document]):
    """Repository for Document entities."""

    model = Document

    async def get_by_source_path(self, source_path: str) -> Optional[Document]:
        """
        Get document by source path.

        Args:
            source_path: Source path of document

        Returns:
            Document or None if not found
        """
        stmt = select(Document).where(Document.source_path == source_path)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_file_hash(self, file_hash: str) -> Optional[Document]:
        """
        Get document by file hash.

        Args:
            file_hash: File hash of document

        Returns:
            Document or None if not found
        """
        stmt = select(Document).where(Document.file_hash == file_hash)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_status(self, status: str) -> List[Document]:
        """
        List documents by processing status.

        Args:
            status: Processing status

        Returns:
            List of documents with specified status
        """
        stmt = select(Document).where(Document.processing_status == status)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def list_chunks(self, document_id: str) -> List[Chunk]:
        """
        List chunks for a document.

        Args:
            document_id: Document ID

        Returns:
            List of chunks for the document
        """
        stmt = select(Chunk).where(Chunk.document_id == document_id)
        result = await self.session.execute(stmt)
        return list(result.scalars())


class CategoryRepository(BaseRepository[Category]):
    """Repository for Category entities."""

    model = Category

    async def get_by_slug(self, slug: str) -> Optional[Category]:
        """
        Get category by slug.

        Args:
            slug: Category slug

        Returns:
            Category or None if not found
        """
        stmt = select(Category).where(Category.slug == slug)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_root_categories(self) -> List[Category]:
        """
        Get root categories (those with no parent).

        Returns:
            List of root categories
        """
        stmt = select(Category).where(Category.parent_id.is_(None))
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def get_children(self, parent_id: str) -> List[Category]:
        """
        Get child categories of a parent.

        Args:
            parent_id: Parent category ID

        Returns:
            List of child categories
        """
        stmt = select(Category).where(Category.parent_id == parent_id)
        result = await self.session.execute(stmt)
        return list(result.scalars())


class ChunkRepository(BaseRepository[Chunk]):
    """Repository for Chunk entities."""

    model = Chunk

    async def list_by_document(self, document_id: str) -> List[Chunk]:
        """
        List chunks by document ID.

        Args:
            document_id: Document ID

        Returns:
            List of chunks for the document
        """
        stmt = select(Chunk).where(Chunk.document_id == document_id)
        result = await self.session.execute(stmt)
        return list(result.scalars())


class DocumentTagRepository(BaseRepository[DocumentTag]):
    """Repository for DocumentTag entities."""

    model = DocumentTag

    async def list_by_document(self, document_id: str) -> List[DocumentTag]:
        """
        List tags by document ID.

        Args:
            document_id: Document ID

        Returns:
            List of document tags
        """
        stmt = select(DocumentTag).where(DocumentTag.document_id == document_id)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def list_by_category(self, category_id: str) -> List[DocumentTag]:
        """
        List tags by category ID.

        Args:
            category_id: Category ID

        Returns:
            List of document tags
        """
        stmt = select(DocumentTag).where(DocumentTag.category_id == category_id)
        result = await self.session.execute(stmt)
        return list(result.scalars())


class WikiPageRepository(BaseRepository[WikiPage]):
    """Repository for WikiPage entities."""

    model = WikiPage

    async def get_by_title(self, title: str) -> Optional[WikiPage]:
        """
        Get wiki page by title.

        Args:
            title: Page title

        Returns:
            Wiki page or None if not found
        """
        stmt = select(WikiPage).where(WikiPage.title == title)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
