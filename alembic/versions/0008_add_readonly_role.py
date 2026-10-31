"""Add the grimoire_readonly role used by the MCP pg_query tool.

``grimoire_pg_query`` lets DEV/AGENT keys run ad-hoc SELECTs. Its regex
guards can be talked around: ``query_to_xml('select * from api_' || 'keys',
...)`` builds the query at run time, so the regex never sees the table name.
A database role is a boundary that can't be talked around, so pg_query now
connects as this role (audit findings C-1/M-2):

* SELECT on every table in ``public`` (and, via default privileges, on tables
  later migrations create), except ``api_keys``, which it can't read at all.
* No INSERT/UPDATE/DELETE privileges anywhere. The role also defaults to
  read-only transactions with a 30s ``statement_timeout``. A session may SET
  those itself, but that buys nothing: privileges still refuse every write,
  and pg_query runs one statement, whose timeout is fixed when it starts (a
  ``set_config('statement_timeout', '0', ...)`` inside it doesn't stop it).
* Created NOLOGIN. ``grimoire db readonly-role`` (run by the ``db-migrate``
  container) gives it a password from ``POSTGRES_READONLY_PASSWORD``; until
  then pg_query refuses to run.

A later migration that adds a table holding secrets must
``REVOKE ALL ON <table> FROM grimoire_readonly``, as this one does for
``api_keys``: the default privileges grant SELECT on new tables.

Needs a migration user allowed to create roles (the Postgres image's
POSTGRES_USER is a superuser). No-op on SQLite.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-31 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "grimoire_readonly"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN;
            END IF;
            EXECUTE format(
                'GRANT CONNECT ON DATABASE %I TO {ROLE}', current_database()
            );
        END
        $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {ROLE}"
    )
    op.execute(f"REVOKE ALL ON api_keys FROM {ROLE}")
    op.execute(f"ALTER ROLE {ROLE} SET default_transaction_read_only = on")
    op.execute(f"ALTER ROLE {ROLE} SET statement_timeout = '30s'")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM {ROLE}"
    )
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {ROLE}")
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format(
                'REVOKE ALL ON DATABASE %I FROM {ROLE}', current_database()
            );
        END
        $$;
        """
    )
    op.execute(f"DROP ROLE IF EXISTS {ROLE}")
