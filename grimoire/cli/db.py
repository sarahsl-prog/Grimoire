"""Database administration commands.

``grimoire db readonly-role`` turns on the ``grimoire_readonly`` role (created
NOLOGIN by migration 0008) with the password in ``POSTGRES_READONLY_PASSWORD``,
so the MCP ``pg_query`` tool can connect as it. The ``db-migrate`` container
runs it after ``alembic upgrade head`` on every deploy, so rotating the
password is: change it in ``.env``, redeploy.
"""

from __future__ import annotations

import os
import re

import click

from grimoire.cli.helpers import (
    async_command,
    echo_error,
    echo_success,
    get_db_context,
    setup_db,
    teardown_db,
)

ROLE = "grimoire_readonly"
# The variable's name, not a password.
PASSWORD_ENV = "POSTGRES_READONLY_PASSWORD"  # noqa: S105  # nosec B105

# Letters, digits, '-' and '_' only, so the password needs no quoting in SQL
# and no percent-encoding in the connection URL compose builds from it.
# `openssl rand -hex 24` produces one.
_PASSWORD_RE = re.compile(r"[A-Za-z0-9_-]{24,128}")


@click.group("db")
def db_group() -> None:
    """Database administration."""


@db_group.command("readonly-role")
@async_command
async def readonly_role() -> None:
    """Enable login for the pg_query role from POSTGRES_READONLY_PASSWORD.

    Idempotent. Does nothing when the variable is unset (pg_query then stays
    disabled). Run after migrations; the db-migrate container does this.
    """
    password = os.environ.get(PASSWORD_ENV, "")
    if not password:
        click.echo(f"{PASSWORD_ENV} is not set; pg_query stays disabled.")
        return
    if not _PASSWORD_RE.fullmatch(password):
        echo_error(
            f"{PASSWORD_ENV} must be 24-128 characters of letters, digits, '-' "
            "or '_' (try `openssl rand -hex 24`)."
        )
        raise SystemExit(1)

    from sqlalchemy import text

    await setup_db()
    try:
        async with get_db_context() as db:
            exists = (
                await db.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                    {"role": ROLE},
                )
            ).scalar()
            if not exists:
                echo_error(f"Role {ROLE} not found; run `alembic upgrade head` first.")
                raise SystemExit(1)
            # ALTER ROLE takes no bind parameters. The password was checked
            # against _PASSWORD_RE above, so it can't contain a quote.
            await db.execute(text(f"ALTER ROLE {ROLE} LOGIN PASSWORD '{password}'"))
    finally:
        await teardown_db()
    echo_success(f"{ROLE} can log in; pg_query is enabled.")
