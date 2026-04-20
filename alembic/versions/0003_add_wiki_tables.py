"""Add wiki tables for wiki tool feature.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-20 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    is_postgresql = dialect == "postgresql"

    # Create enum types
    wiki_page_status_values = ["draft", "compiled", "flagged"]
    wiki_ref_type_values = ["references", "depends_on", "related_to", "contradicts"]
    compile_status_values = ["pending", "compiling", "completed", "failed"]

    # --- wiki_pages ---
    op.create_table(
        "wiki_pages",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("slug", sa.String(512), nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "status",
            sa.Enum(*wiki_page_status_values, name="wiki_page_status_enum", create_type=True)
            if is_postgresql else sa.String(50),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("entity_type", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wiki_pages")),
        sa.UniqueConstraint("title", name=op.f("uq_wiki_pages_title")),
        sa.UniqueConstraint("slug", name=op.f("uq_wiki_pages_slug")),
    )
    op.create_index(op.f("ix_wiki_pages_slug"), "wiki_pages", ["slug"])
    op.create_index("ix_wiki_pages_slug_status", "wiki_pages", ["slug", "status"])

    # --- wiki_page_sections ---
    op.create_table(
        "wiki_page_sections",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("wiki_page_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("heading", sa.String(512), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("section_index", sa.Integer(), nullable=False),
        sa.Column("source_document_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("source_priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contradiction_flag", sa.Text, nullable=True),
        sa.Column("superseded_by_section_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["wiki_page_id"], ["wiki_pages.id"],
            name=op.f("fk_wiki_page_sections_wiki_page_id_wiki_pages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["documents.id"],
            name=op.f("fk_wiki_page_sections_source_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_section_id"], ["wiki_page_sections.id"],
            name=op.f("fk_wiki_page_sections_superseded_by_section_id_wiki_page_sections"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wiki_page_sections")),
    )
    op.create_index(op.f("ix_wiki_page_sections_wiki_page_id"), "wiki_page_sections", ["wiki_page_id"])
    op.create_index(
        "ix_wiki_sections_page_index", "wiki_page_sections", ["wiki_page_id", "section_index"]
    )

    # --- wiki_cross_references ---
    op.create_table(
        "wiki_cross_references",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("source_page_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("target_page_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "ref_type",
            sa.Enum(*wiki_ref_type_values, name="wiki_ref_type_enum", create_type=True)
            if is_postgresql else sa.String(50),
            nullable=False,
        ),
        sa.Column("context", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_page_id"], ["wiki_pages.id"],
            name=op.f("fk_wiki_cross_references_source_page_id_wiki_pages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_page_id"], ["wiki_pages.id"],
            name=op.f("fk_wiki_cross_references_target_page_id_wiki_pages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wiki_cross_references")),
        sa.UniqueConstraint(
            "source_page_id", "target_page_id", "ref_type",
            name="uq_wiki_cross_ref_unique",
        ),
    )
    op.create_index(
        op.f("ix_wiki_cross_references_source_page_id"),
        "wiki_cross_references", ["source_page_id"],
    )
    op.create_index(
        op.f("ix_wiki_cross_references_target_page_id"),
        "wiki_cross_references", ["target_page_id"],
    )

    # --- wiki_compile_jobs ---
    op.create_table(
        "wiki_compile_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*compile_status_values, name="compile_status_enum", create_type=True)
            if is_postgresql else sa.String(50),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"],
            name=op.f("fk_wiki_compile_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wiki_compile_jobs")),
    )
    op.create_index(
        op.f("ix_wiki_compile_jobs_document_id"), "wiki_compile_jobs", ["document_id"]
    )
    op.create_index("ix_wiki_compile_jobs_status", "wiki_compile_jobs", ["status"])


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    is_postgresql = dialect == "postgresql"

    op.drop_table("wiki_compile_jobs")
    op.drop_table("wiki_cross_references")
    op.drop_table("wiki_page_sections")
    op.drop_table("wiki_pages")

    if is_postgresql:
        op.execute("DROP TYPE IF EXISTS compile_status_enum CASCADE")
        op.execute("DROP TYPE IF EXISTS wiki_ref_type_enum CASCADE")
        op.execute("DROP TYPE IF EXISTS wiki_page_status_enum CASCADE")