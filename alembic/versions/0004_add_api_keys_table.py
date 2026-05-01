"""Add api_keys table for tiered API key authentication.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-01 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    is_postgresql = dialect == "postgresql"

    api_key_tier_values = ["agt", "dvl", "rdl"]

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column(
            "tier",
            sa.Enum(*api_key_tier_values, name="api_key_tier_enum", create_type=True)
            if is_postgresql else sa.String(50),
            nullable=False,
        ),
        sa.Column("key_prefix", sa.String(12), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_prefix", name=op.f("uq_api_keys_key_prefix")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
    )
    op.create_index(op.f("ix_api_keys_tier"), "api_keys", ["tier"])
    op.create_index("ix_api_keys_tier_created", "api_keys", ["tier", "created_at"])
    op.create_index(
        op.f("ix_api_keys_key_prefix"), "api_keys", ["key_prefix"]
    )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    is_postgresql = dialect == "postgresql"

    op.drop_table("api_keys")

    if is_postgresql:
        op.execute("DROP TYPE IF EXISTS api_key_tier_enum CASCADE")