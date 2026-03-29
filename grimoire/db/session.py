"""Database session management utilities."""

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DatabaseSessionManager:
    """Manages async database sessions.

    Provides:
    - Async engine creation
    - Session factory
    - Context managers for session lifecycle
    """

    def __init__(self) -> None:
        """Initialize session manager (not connected yet)."""
        self._engine: Optional[Any] = None
        self._session_maker: Optional[async_sessionmaker[AsyncSession]] = None

    async def initialize(self, database_url: str, pool_size: int = 10) -> None:
        """Initialize the database engine and session maker.

        Args:
            database_url: Async PostgreSQL URL (postgresql+asyncpg://...)
            pool_size: Connection pool size
        """
        from sqlalchemy.ext.asyncio import create_async_engine as _create_engine

        self._engine = _create_engine(
            database_url,
            pool_size=pool_size,
            max_overflow=20,
            pool_pre_ping=True,
            echo=False,
        )
        self._session_maker = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )

    async def close(self) -> None:
        """Close the database engine."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_maker = None

    async def create_session(self) -> AsyncSession:
        """Create a new async session.

        Returns:
            New AsyncSession instance

        Raises:
            RuntimeError: If database not initialized
        """
        if self._session_maker is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._session_maker()

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Context manager for database sessions.

        Automatically handles commit/rollback.

        Example:
            async with db_manager.session() as session:
                result = await session.execute(select(...))
        """
        if self._session_maker is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        async with self._session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    @property
    def engine(self) -> Optional[Any]:
        """Get the SQLAlchemy engine (if initialized)."""
        return self._engine


_db_manager: Optional[DatabaseSessionManager] = None


def get_db_manager() -> DatabaseSessionManager:
    """Get the global database session manager.

    Returns:
        DatabaseSessionManager instance

    Raises:
        RuntimeError: If database not initialized
    """
    if _db_manager is None:
        raise RuntimeError("Database not initialized. Call initialize_db() first.")
    return _db_manager


async def initialize_db(database_url: str, pool_size: int = 10) -> None:
    """Initialize the database connection.

    Args:
        database_url: Async PostgreSQL URL
        pool_size: Connection pool size
    """
    global _db_manager
    _db_manager = DatabaseSessionManager()
    await _db_manager.initialize(database_url, pool_size)


async def close_db() -> None:
    """Close the database connection."""
    global _db_manager
    if _db_manager:
        await _db_manager.close()
        _db_manager = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Get a database session (for FastAPI dependency injection).

    Yields:
        AsyncSession with automatic commit/rollback

    Example:
        @app.get("/items/")
        async def get_items(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(Item))
            return result.scalars().all()
    """
    manager = get_db_manager()
    async with manager.session() as session:
        yield session


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Get a database session as an async context manager.

    Use this when you need a session outside of FastAPI dependency injection.

    Example:
        async with get_db_context() as db:
            result = await db.execute(select(Document))
            docs = result.scalars().all()
    """
    manager = get_db_manager()
    async with manager.session() as session:
        yield session
