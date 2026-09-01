"""Add GIN index on security_metadata for JSONB array queries.

Adding a GIN (Generalized Inverted Index) index on the security_metadata JSONB
column enables O(log n) lookups when filtering by JSONB array fields
(platforms, log_sources, playbook_phase, etc.) using the @> containment
operator.  Without this index, JSONB array membership queries do a full
sequential scan of the column.

This is particularly important for grimoire_search_playbook's facet-only path
and SQL pre-filtering in grimoire_search_cve, where list-facet conditions
are evaluated against the security_metadata blob on large document corpora.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-01 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.create_index(
            "ix_documents_security_metadata_gin",
            "documents",
            ["security_metadata"],
            postgresql_using="gin",
            postgresql_with={"type": "jsonb_path_ops"},
        )
    else:
        op.create_index(
            "ix_documents_security_metadata_gin",
            "documents",
            ["security_metadata"],
            if_not_exists=True,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_documents_security_metadata_gin",
        table_name="documents",
    )
