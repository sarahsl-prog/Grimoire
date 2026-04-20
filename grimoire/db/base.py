"""SQLAlchemy Base configuration for async PostgreSQL."""

from uuid import uuid4

from sqlalchemy import MetaData
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Naming convention for constraints (helps with migrations)
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=convention)


class Base(AsyncAttrs, DeclarativeBase):
    """Base class for all SQLAlchemy models.

    Provides:
    - UUID primary key with default uuid4 generation
    - Async capabilities via AsyncAttrs
    - Consistent table naming convention
    """

    metadata = metadata

    # Generic repr for debugging
    def __repr__(self) -> str:
        attrs = []
        for key in ["id", "name", "title", "source_path"]:
            if hasattr(self, key):
                value = getattr(self, key)
                attrs.append(f"{key}={value!r}")
                break
        return f"<{self.__class__.__name__}({', '.join(attrs)})>"

    def __str__(self) -> str:
        return self.__repr__()


class UUIDMixin:
    """Mixin that adds a UUID primary key column."""

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid4()),
    )


class TimestampMixin:
    """Mixin that adds created_at timestamp."""

    from datetime import datetime, timezone

    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
