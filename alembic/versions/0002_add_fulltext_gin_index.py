"""Add GIN index for full-text search on chunks.content.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-29 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create GIN index for full-text search."""
    dialect = op.get_bind().dialect.name
    is_postgresql = dialect == "postgresql"

    if is_postgresql:
        # Create GIN index on chunks.content using to_tsvector
        # This enables fast full-text search queries
        op.execute("""
            CREATE INDEX ix_chunks_content_fts
            ON chunks
            USING GIN (to_tsvector('english', content))
        """)

        # Optional: Create GIN index on documents.title
        op.execute("""
            CREATE INDEX ix_documents_title_fts
            ON documents
            USING GIN (to_tsvector('english', title))
        """)
    else:
        # For SQLite (or other backends), use a standard index
        # Note: SQLite FTS5 would require a virtual table
        op.create_index(
            'ix_chunks_content_fts',
            'chunks',
            ['content'],
            postgresql_using='GIN' if is_postgresql else None
        )


def downgrade() -> None:
    """Remove GIN indexes."""
    dialect = op.get_bind().dialect.name
    is_postgresql = dialect == "postgresql"

    if is_postgresql:
        op.execute("DROP INDEX IF EXISTS ix_chunks_content_fts")
        op.execute("DROP INDEX IF EXISTS ix_documents_title_fts")
    else:
        op.drop_index('ix_chunks_content_fts', table_name='chunks')
