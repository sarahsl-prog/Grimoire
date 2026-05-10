"""Migration smoke tests against an ephemeral SQLite database.

Phase 2 introduces ``alembic/versions/0006_add_security_metadata.py``.
This test exercises the full upgrade/downgrade/upgrade cycle on a fresh
sqlite file:

1. ``alembic upgrade head`` — schema gains the seven Phase-2 columns and
   seven new indexes;
2. ``alembic downgrade -1`` — schema loses them again;
3. ``alembic upgrade head`` — re-applies cleanly.

We bypass the project's async alembic env (which is asyncpg-only) by
constructing a tiny synchronous test env in-place: the alembic
``script_location`` points at the repo's ``alembic/`` directory so the
real version files are picked up, while a per-test ``env.py`` runs the
migrations against the sqlite DB. This keeps the test self-contained
(no docker, no postgres) and exercises the actual migration scripts
shipped in the repo.
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
from pathlib import Path
from typing import Iterator, List

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


# Columns added by 0006 — kept in sync with the migration / model.
_PHASE2_COLUMNS: tuple[str, ...] = (
    "source_type",
    "cve_id",
    "severity",
    "mitre_technique_id",
    "tlp_level",
    "content_date",
    "security_metadata",
)

# Indexes created by 0006.
_PHASE2_INDEXES: tuple[str, ...] = (
    "ix_documents_source_type",
    "ix_documents_cve_id",
    "ix_documents_severity",
    "ix_documents_mitre_technique_id",
    "ix_documents_content_date",
    "ix_documents_severity_content_date",
    "ix_documents_source_type_severity",
)


# Synchronous env.py used in lieu of the project's async one. The script
# location below copies version files in via Alembic's normal lookup, so
# all migrations 0001..0006 run against the temp sqlite DB.
_SYNC_ENV_PY = """
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import grimoire models so they register with the shared metadata.
from grimoire.db.base import Base
import grimoire.db.models  # noqa: F401  -- side effects only

target_metadata = Base.metadata

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      compare_type=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
"""


@pytest.fixture()
def sqlite_alembic_cfg() -> Iterator[Config]:
    """Yield an alembic Config wired to a temp sqlite DB."""

    repo_root = Path(__file__).resolve().parents[1]
    tmp_dir = Path(tempfile.mkdtemp(prefix="phase2_alembic_"))
    sqlite_file = tmp_dir / "smoke.sqlite"
    url = f"sqlite:///{sqlite_file}"

    # Mirror the real script layout: script_location with versions/.
    script_dir = tmp_dir / "alembic"
    script_dir.mkdir()
    (script_dir / "env.py").write_text(_SYNC_ENV_PY)
    # Symlink the real versions directory so we run the actual migrations.
    versions_target = repo_root / "alembic" / "versions"
    (script_dir / "versions").symlink_to(versions_target)
    # script.py.mako is required by alembic's revision command, but
    # `upgrade` doesn't need it — keep it absent.

    cfg = Config()
    cfg.set_main_option("script_location", str(script_dir))
    cfg.set_main_option("sqlalchemy.url", url)

    yield cfg

    shutil.rmtree(tmp_dir, ignore_errors=True)


def _list_columns(engine: sa.Engine, table: str) -> List[str]:
    insp = sa.inspect(engine)
    return [col["name"] for col in insp.get_columns(table)]


def _list_indexes(engine: sa.Engine, table: str) -> List[str]:
    insp = sa.inspect(engine)
    return [idx["name"] for idx in insp.get_indexes(table)]


# ---------------------------------------------------------------------------
# Lightweight sanity test (loads the migration module directly by path).
# ---------------------------------------------------------------------------


def test_migration_module_revision_strings() -> None:
    """``0006`` declares the right revision metadata."""

    repo_root = Path(__file__).resolve().parents[1]
    mig_path = repo_root / "alembic" / "versions" / "0006_add_security_metadata.py"
    spec = importlib.util.spec_from_file_location("_phase2_migration", mig_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0006"
    assert mod.down_revision == "0005"


# ---------------------------------------------------------------------------
# Full upgrade / downgrade / re-upgrade cycle.
# ---------------------------------------------------------------------------


def test_upgrade_downgrade_upgrade_cycle(sqlite_alembic_cfg: Config) -> None:
    url = sqlite_alembic_cfg.get_main_option("sqlalchemy.url")
    assert url is not None

    # Round 1: blank DB → head
    command.upgrade(sqlite_alembic_cfg, "head")

    engine = sa.create_engine(url)
    columns = _list_columns(engine, "documents")
    indexes = _list_indexes(engine, "documents")
    for col in _PHASE2_COLUMNS:
        assert col in columns, f"column {col!r} missing after upgrade head"
    for idx in _PHASE2_INDEXES:
        assert idx in indexes, f"index {idx!r} missing after upgrade head"

    # Round 2: head → -1 (back to 0005)
    engine.dispose()
    command.downgrade(sqlite_alembic_cfg, "-1")

    engine = sa.create_engine(url)
    columns = _list_columns(engine, "documents")
    indexes = _list_indexes(engine, "documents")
    for col in _PHASE2_COLUMNS:
        assert col not in columns, f"column {col!r} should be gone after downgrade"
    for idx in _PHASE2_INDEXES:
        assert idx not in indexes, f"index {idx!r} should be gone after downgrade"

    # Round 3: re-upgrade should succeed
    engine.dispose()
    command.upgrade(sqlite_alembic_cfg, "head")

    engine = sa.create_engine(url)
    columns = _list_columns(engine, "documents")
    indexes = _list_indexes(engine, "documents")
    for col in _PHASE2_COLUMNS:
        assert col in columns
    for idx in _PHASE2_INDEXES:
        assert idx in indexes
    engine.dispose()
