"""The grimoire_readonly role against a real PostgreSQL (migration 0008).

Mocks can't show what a database role allows, so these run the migrations on
a real server and then attack it as the role, the way a pg_query caller would.

Skipped unless ``GRIMOIRE_TEST_PG_ADMIN_URL`` points at a database the test
may migrate, as a role allowed to create roles, e.g.
``postgresql+asyncpg://grimoire:pw@127.0.0.1:5432/grimoire_test``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")

ADMIN_URL = os.environ.get("GRIMOIRE_TEST_PG_ADMIN_URL", "")
REPO_ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "integration_test_readonly_0123456789"

pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="GRIMOIRE_TEST_PG_ADMIN_URL not set"
)


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="module")
def migrated() -> None:
    env = {**os.environ, "GRIMOIRE_DATABASE__URL": ADMIN_URL}
    result = subprocess.run(  # noqa: S603 — fixed argv, our own interpreter
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
async def as_readonly(migrated: None) -> AsyncIterator[object]:
    admin = await asyncpg.connect(_dsn(ADMIN_URL))
    try:
        await admin.execute(f"ALTER ROLE grimoire_readonly LOGIN PASSWORD '{PASSWORD}'")
        host = admin._addr[0] if isinstance(admin._addr, tuple) else "127.0.0.1"
        port = admin._addr[1] if isinstance(admin._addr, tuple) else 5432
        database = await admin.fetchval("select current_database()")
    finally:
        await admin.close()
    conn = await asyncpg.connect(
        host=host,
        port=port,
        user="grimoire_readonly",
        password=PASSWORD,
        database=database,
    )
    try:
        yield conn
    finally:
        await conn.close()


async def test_can_read_documents(as_readonly: object) -> None:
    assert await as_readonly.fetchval("select count(*) >= 0 from documents")  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "sql",
    [
        "select * from api_keys",
        # The bypass the regex guard missed: the table name is built at run time.
        "select query_to_xml('select * from api_' || 'keys', true, true, '')",
        "select pg_read_file('/etc/passwd')",
    ],
)
async def test_cannot_read_secrets(as_readonly: object, sql: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await as_readonly.fetch(sql)  # type: ignore[attr-defined]


async def test_cannot_write_even_in_a_read_write_transaction(
    as_readonly: object,
) -> None:
    conn = as_readonly
    await conn.execute("set default_transaction_read_only = off")  # type: ignore[attr-defined]
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with conn.transaction(readonly=False):  # type: ignore[attr-defined]
            await conn.execute("delete from documents")  # type: ignore[attr-defined]


async def test_role_defaults(as_readonly: object) -> None:
    conn = as_readonly
    assert await conn.fetchval("show default_transaction_read_only") == "on"  # type: ignore[attr-defined]
    assert await conn.fetchval("show statement_timeout") == "30s"  # type: ignore[attr-defined]
