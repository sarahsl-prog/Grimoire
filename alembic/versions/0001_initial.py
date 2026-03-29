"""Initial migration: Create all base tables.

Revision ID: 0001
Revises:
Create Date: 2026-03-29 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_json_column(nullable: bool = True) -> sa.Column:
    """Get a JSON column that works with both PostgreSQL (JSONB) and SQLite (JSON)."""
    # Use dialect-specific type
    return sa.Column(
        sa.dialects.postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON(),
        nullable=nullable,
    )


def upgrade() -> None:
    """Create all tables."""
    dialect = op.get_bind().dialect.name
    is_postgresql = dialect == "postgresql"

    # Create enum types first (PostgreSQL only)
    if is_postgresql:
        op.execute("""
            CREATE TYPE storage_backend_enum AS ENUM (
                'local', 'usb', 'rclone', 'gdrive', 'onedrive'
            )
        """)
        op.execute("""
            CREATE TYPE file_type_enum AS ENUM (
                'pdf', 'docx', 'pptx', 'xlsx', 'html', 'md', 'txt',
                'image', 'audio', 'video', 'other'
            )
        """)
        op.execute("""
            CREATE TYPE processing_status_enum AS ENUM (
                'pending', 'processing', 'completed', 'failed', 'stale'
            )
        """)
        op.execute("""
            CREATE TYPE content_type_enum AS ENUM (
                'summary', 'flash_card', 'cliff_notes', 'outline', 'image', 'extract'
            )
        """)
        op.execute("""
            CREATE TYPE relationship_type_enum AS ENUM (
                'related', 'references', 'summarizes', 'derived_from', 'similar'
            )
        """)
        op.execute("""
            CREATE TYPE tagged_by_enum AS ENUM ('llm', 'user', 'rule')
        """)
        op.execute("""
            CREATE TYPE discovered_by_enum AS ENUM ('llm', 'user', 'manual')
        """)
        op.execute("""
            CREATE TYPE action_type_enum AS ENUM (
                'discovered', 'extracted', 'chunked', 'tagged', 'failed'
            )
        """)
        op.execute("""
            CREATE TYPE status_type_enum AS ENUM ('success', 'partial', 'failed')
        """)
        op.execute("""
            CREATE TYPE cache_type_enum AS ENUM ('query', 'search', 'generated')
        """)

    # documents table
    op.create_table(
        'documents',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('source_path', sa.String(length=2048), nullable=False),
        sa.Column(
            'storage_backend',
            sa.Enum('local', 'usb', 'rclone', 'gdrive', 'onedrive',
                    name='storage_backend_enum') if is_postgresql else sa.String(20),
            nullable=False,
        ),
        sa.Column(
            'file_type',
            sa.Enum('pdf', 'docx', 'pptx', 'xlsx', 'html', 'md', 'txt',
                    'image', 'audio', 'video', 'other',
                    name='file_type_enum') if is_postgresql else sa.String(10),
            nullable=False,
        ),
        sa.Column('file_hash', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'processing_status',
            sa.Enum('pending', 'processing', 'completed',
                    'failed', 'stale',
                    name='processing_status_enum') if is_postgresql else sa.String(20),
            default='pending' if not is_postgresql else None,
            nullable=False,
        ),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_documents')),
        sa.UniqueConstraint('file_hash', name=op.f('uq_documents_file_hash'))
    )
    op.create_index(op.f('ix_documents_file_hash'), 'documents', ['file_hash'])
    op.create_index(op.f('ix_documents_source_path'), 'documents', ['source_path'])
    op.create_index(op.f('ix_documents_created_at'), 'documents', ['created_at'])
    op.create_index(op.f('ix_documents_processing_status'), 'documents',
                    ['processing_status'])
    op.create_index('ix_documents_status_created', 'documents',
                    ['processing_status', 'created_at'])

    # chunks table
    op.create_table(
        'chunks',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('document_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('token_count', sa.Integer(), nullable=False),
        sa.Column('vector_id', sa.String(length=256), nullable=True),
        sa.Column('embedding_model', sa.String(length=128), nullable=True),
        sa.Column('prev_chunk_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('next_chunk_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'],
                                name=op.f('fk_chunks_document_id_documents'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['prev_chunk_id'], ['chunks.id'],
                                name=op.f('fk_chunks_prev_chunk_id_chunks'),
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['next_chunk_id'], ['chunks.id'],
                                name=op.f('fk_chunks_next_chunk_id_chunks'),
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_chunks')),
        sa.UniqueConstraint('document_id', 'chunk_index',
                            name=op.f('uq_chunks_document_id_chunk_index'))
    )
    op.create_index(op.f('ix_chunks_document_id'), 'chunks', ['document_id'])
    op.create_index(op.f('ix_chunks_prev_chunk_id'), 'chunks', ['prev_chunk_id'])
    op.create_index(op.f('ix_chunks_next_chunk_id'), 'chunks', ['next_chunk_id'])
    op.create_index('ix_chunks_doc_idx', 'chunks', ['document_id', 'chunk_index'])

    # categories table
    op.create_table(
        'categories',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('slug', sa.String(length=256), nullable=False),
        sa.Column('parent_id', postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('color', sa.String(length=7), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['parent_id'], ['categories.id'],
                                name=op.f('fk_categories_parent_id_categories'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_categories')),
        sa.UniqueConstraint('slug', name=op.f('uq_categories_slug'))
    )
    op.create_index(op.f('ix_categories_parent_id'), 'categories', ['parent_id'])

    # document_tags table (many-to-many junction)
    op.create_table(
        'document_tags',
        sa.Column('document_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('category_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column(
            'tagged_by',
            sa.Enum('llm', 'user', 'rule', name='tagged_by_enum') if is_postgresql else sa.String(10),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'],
                                name=op.f('fk_document_tags_document_id_documents'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'],
                                name=op.f('fk_document_tags_category_id_categories'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('document_id', 'category_id',
                                name=op.f('pk_document_tags'))
    )

    # generated_content table
    json_type = postgresql.JSONB if is_postgresql else sa.JSON
    op.create_table(
        'generated_content',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('document_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            'content_type',
            sa.Enum('summary', 'flash_card', 'cliff_notes',
                    'outline', 'image', 'extract',
                    name='content_type_enum') if is_postgresql else sa.String(20),
            nullable=False,
        ),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('model_used', sa.String(length=128), nullable=False),
        sa.Column('generation_params', json_type(astext_type=sa.Text()) if is_postgresql else json_type(), nullable=True),
        sa.Column('cache_hit', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'],
                                name=op.f('fk_generated_content_document_id_documents'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_generated_content'))
    )
    op.create_index(op.f('ix_generated_content_document_id'), 'generated_content',
                    ['document_id'])
    op.create_index('ix_generated_content_type', 'generated_content',
                    ['document_id', 'content_type'])

    # relationships table
    op.create_table(
        'relationships',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('source_document_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('target_document_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            'relationship_type',
            sa.Enum('related', 'references', 'summarizes',
                    'derived_from', 'similar',
                    name='relationship_type_enum') if is_postgresql else sa.String(20),
            nullable=False,
        ),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column(
            'discovered_by',
            sa.Enum('llm', 'user', 'manual',
                    name='discovered_by_enum') if is_postgresql else sa.String(10),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['source_document_id'], ['documents.id'],
                                name=op.f('fk_relationships_source_document_id_documents'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_document_id'], ['documents.id'],
                                name=op.f('fk_relationships_target_document_id_documents'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_relationships')),
        sa.UniqueConstraint('source_document_id', 'target_document_id', 'relationship_type',
                            name=op.f('uq_relationships_source_document_id_target_document_id_relationship_type'))
    )
    op.create_index(op.f('ix_relationships_source_document_id'), 'relationships',
                    ['source_document_id'])
    op.create_index(op.f('ix_relationships_target_document_id'), 'relationships',
                    ['target_document_id'])
    op.create_index('ix_relationships_source_type', 'relationships',
                    ['source_document_id', 'relationship_type'])

    # watch_paths table
    op.create_table(
        'watch_paths',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('path', sa.String(length=2048), nullable=False),
        sa.Column(
            'storage_backend',
            sa.Enum('local', 'usb', 'rclone', 'gdrive', 'onedrive',
                    name='storage_backend_enum') if is_postgresql else sa.String(20),
            nullable=False,
        ),
        sa.Column('recursive', sa.Boolean(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('last_scanned_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('poll_interval_seconds', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_watch_paths')),
        sa.UniqueConstraint('path', 'storage_backend', name=op.f('uq_watch_paths_path_storage_backend'))
    )

    # processing_log table
    op.create_table(
        'processing_log',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('document_id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            'action',
            sa.Enum('discovered', 'extracted', 'chunked', 'tagged', 'failed',
                    name='action_type_enum') if is_postgresql else sa.String(20),
            nullable=False,
        ),
        sa.Column(
            'status',
            sa.Enum('success', 'partial', 'failed', name='status_type_enum') if is_postgresql else sa.String(10),
            nullable=False,
        ),
        sa.Column('details', json_type(astext_type=sa.Text()) if is_postgresql else json_type(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'],
                                name=op.f('fk_processing_log_document_id_documents'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_processing_log'))
    )
    op.create_index(op.f('ix_processing_log_document_id'), 'processing_log',
                    ['document_id'])
    op.create_index(op.f('ix_processing_log_created_at'), 'processing_log',
                    ['created_at'])
    op.create_index('ix_processing_log_doc_action', 'processing_log',
                    ['document_id', 'action'])

    # cache_entries table
    op.create_table(
        'cache_entries',
        sa.Column('id', postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column('cache_key', sa.String(length=512), nullable=False),
        sa.Column(
            'cache_type',
            sa.Enum('query', 'search', 'generated', name='cache_type_enum') if is_postgresql else sa.String(10),
            nullable=False,
        ),
        sa.Column('data', json_type(astext_type=sa.Text()) if is_postgresql else json_type(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('hit_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_cache_entries')),
        sa.UniqueConstraint('cache_key', name=op.f('uq_cache_entries_cache_key'))
    )
    op.create_index(op.f('ix_cache_entries_cache_key'), 'cache_entries',
                    ['cache_key'])
    op.create_index(op.f('ix_cache_entries_cache_type'), 'cache_entries',
                    ['cache_type'])
    op.create_index(op.f('ix_cache_entries_expires_at'), 'cache_entries',
                    ['expires_at'])


def downgrade() -> None:
    """Drop all tables and enums."""
    dialect = op.get_bind().dialect.name
    is_postgresql = dialect == "postgresql"

    # Drop tables in reverse order (respecting foreign keys)
    op.drop_table('cache_entries')
    op.drop_table('processing_log')
    op.drop_table('watch_paths')
    op.drop_table('relationships')
    op.drop_table('generated_content')
    op.drop_table('document_tags')
    op.drop_table('categories')
    op.drop_table('chunks')
    op.drop_table('documents')

    # Drop enum types (PostgreSQL only)
    if is_postgresql:
        op.execute('DROP TYPE IF EXISTS cache_type_enum CASCADE')
        op.execute('DROP TYPE IF EXISTS status_type_enum CASCADE')
        op.execute('DROP TYPE IF EXISTS action_type_enum CASCADE')
        op.execute('DROP TYPE IF EXISTS discovered_by_enum CASCADE')
        op.execute('DROP TYPE IF EXISTS tagged_by_enum CASCADE')
        op.execute('DROP TYPE IF EXISTS relationship_type_enum CASCADE')
        op.execute('DROP TYPE IF EXISTS content_type_enum CASCADE')
        op.execute('DROP TYPE IF EXISTS processing_status_enum CASCADE')
        op.execute('DROP TYPE IF EXISTS file_type_enum CASCADE')
        op.execute('DROP TYPE IF EXISTS storage_backend_enum CASCADE')