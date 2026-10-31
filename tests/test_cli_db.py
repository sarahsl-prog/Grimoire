"""grimoire db readonly-role: enable the pg_query role from the environment."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from grimoire.cli.main import cli

GOOD = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def db() -> Any:
    session = AsyncMock()
    exists = MagicMock()
    exists.scalar.return_value = 1
    session.execute = AsyncMock(return_value=exists)

    @asynccontextmanager
    async def _ctx() -> Any:
        yield session

    with (
        patch("grimoire.cli.db.get_db_context", _ctx),
        patch("grimoire.cli.db.setup_db", AsyncMock()),
        patch("grimoire.cli.db.teardown_db", AsyncMock()),
    ):
        yield session


def _run(password: str | None) -> Any:
    env = {} if password is None else {"POSTGRES_READONLY_PASSWORD": password}
    return CliRunner().invoke(cli, ["db", "readonly-role"], env=env)


def test_unset_is_a_no_op(db: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_READONLY_PASSWORD", raising=False)
    result = _run(None)
    assert result.exit_code == 0
    assert "pg_query stays disabled" in result.output
    db.execute.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    ["short", "x" * 129, "has'quote" + "0" * 20, "has space" + "0" * 20, "pct%" * 8],
)
def test_rejects_passwords_that_would_need_quoting(db: AsyncMock, bad: str) -> None:
    result = _run(bad)
    assert result.exit_code == 1
    db.execute.assert_not_called()
    assert bad not in result.output  # never echoed


def test_enables_login(db: AsyncMock) -> None:
    result = _run(GOOD)
    assert result.exit_code == 0, result.output
    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    assert statements[-1] == f"ALTER ROLE grimoire_readonly LOGIN PASSWORD '{GOOD}'"
    assert GOOD not in result.output


def test_missing_role_means_migrations_not_run(db: AsyncMock) -> None:
    missing = MagicMock()
    missing.scalar.return_value = None
    db.execute = AsyncMock(return_value=missing)
    result = _run(GOOD)
    assert result.exit_code == 1
    assert "alembic upgrade head" in result.output
