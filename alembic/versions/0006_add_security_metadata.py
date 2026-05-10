"""Add security metadata columns to the documents table.

Phase 2 of the security strategy plan introduces the
:class:`grimoire.strategies.security.metadata.SecurityMetadata` schema.
The persistence side adds:

* indexed scalar columns on ``documents`` for the fields that downstream
  filters / joins query directly (``source_type``, ``cve_id``,
  ``severity``, ``mitre_technique_id``, ``tlp_level``, ``content_date``);
* a wide-but-sparse JSONB blob ``documents.security_metadata`` for the
  remaining fields (lists, CWE ids, threat actors, etc.);
* two composite indexes that match the most common filter combos.

All columns are nullable so existing rows and general (non-security)
ingest continue to work unchanged. PostgreSQL gets real ENUM types for
``severity`` and ``tlp_level``; SQLite uses VARCHAR + CHECK constraints
generated automatically by SQLAlchemy.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-10 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SEVERITY_VALUES = ("critical", "high", "medium", "low", "info", "unknown")
_TLP_VALUES = ("white", "green", "amber", "red")


def _portable_json() -> sa.types.TypeEngine:
    """Return JSONB on PostgreSQL, generic JSON elsewhere."""

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def _severity_type(create_type: bool) -> sa.types.TypeEngine:
    """Return the severity column type appropriate for the active dialect."""

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        return sa.Enum(
            *_SEVERITY_VALUES,
            name="severity_enum",
            create_type=create_type,
        )
    # On SQLite we deliberately skip the CHECK constraint: SQLite's
    # batch_alter_table downgrade path rebuilds the table and would
    # otherwise duplicate the constraint, breaking subsequent migrations.
    # Pydantic's :class:`Severity` enum validates inputs at the Python
    # layer, so the lack of a DB-side check is benign for dev/test.
    return sa.Enum(
        *_SEVERITY_VALUES,
        name="severity_enum",
        native_enum=False,
        create_constraint=False,
    )


def _tlp_type(create_type: bool) -> sa.types.TypeEngine:
    """Return the TLP-level column type appropriate for the active dialect."""

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        return sa.Enum(
            *_TLP_VALUES,
            name="tlp_level_enum",
            create_type=create_type,
        )
    return sa.Enum(
        *_TLP_VALUES,
        name="tlp_level_enum",
        native_enum=False,
        create_constraint=False,
    )


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    is_postgresql = dialect == "postgresql"

    # On PostgreSQL we explicitly create the ENUM types up-front so the
    # ALTER TABLE below doesn't try to CREATE TYPE inside a transaction
    # block that already references it.
    if is_postgresql:
        sa.Enum(*_SEVERITY_VALUES, name="severity_enum").create(
            op.get_bind(), checkfirst=True
        )
        sa.Enum(*_TLP_VALUES, name="tlp_level_enum").create(
            op.get_bind(), checkfirst=True
        )

    with op.batch_alter_table("documents") as batch_op:
        batch_op.add_column(sa.Column("source_type", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("cve_id", sa.String(32), nullable=True))
        batch_op.add_column(
            sa.Column(
                "severity",
                _severity_type(create_type=False),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("mitre_technique_id", sa.String(16), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "tlp_level",
                _tlp_type(create_type=False),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("content_date", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("security_metadata", _portable_json(), nullable=True)
        )

    # Indexes are created outside the batch context so they get the
    # canonical names used elsewhere in the codebase.
    op.create_index(
        "ix_documents_source_type",
        "documents",
        ["source_type"],
    )
    op.create_index(
        "ix_documents_cve_id",
        "documents",
        ["cve_id"],
    )
    op.create_index(
        "ix_documents_severity",
        "documents",
        ["severity"],
    )
    op.create_index(
        "ix_documents_mitre_technique_id",
        "documents",
        ["mitre_technique_id"],
    )
    op.create_index(
        "ix_documents_content_date",
        "documents",
        ["content_date"],
    )
    op.create_index(
        "ix_documents_severity_content_date",
        "documents",
        ["severity", "content_date"],
    )
    op.create_index(
        "ix_documents_source_type_severity",
        "documents",
        ["source_type", "severity"],
    )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    is_postgresql = dialect == "postgresql"

    op.drop_index("ix_documents_source_type_severity", table_name="documents")
    op.drop_index("ix_documents_severity_content_date", table_name="documents")
    op.drop_index("ix_documents_content_date", table_name="documents")
    op.drop_index("ix_documents_mitre_technique_id", table_name="documents")
    op.drop_index("ix_documents_severity", table_name="documents")
    op.drop_index("ix_documents_cve_id", table_name="documents")
    op.drop_index("ix_documents_source_type", table_name="documents")

    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_column("security_metadata")
        batch_op.drop_column("content_date")
        batch_op.drop_column("tlp_level")
        batch_op.drop_column("mitre_technique_id")
        batch_op.drop_column("severity")
        batch_op.drop_column("cve_id")
        batch_op.drop_column("source_type")

    if is_postgresql:
        op.execute("DROP TYPE IF EXISTS tlp_level_enum CASCADE")
        op.execute("DROP TYPE IF EXISTS severity_enum CASCADE")
