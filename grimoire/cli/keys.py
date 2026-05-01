"""CLI commands for managing API keys."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import click

from grimoire.cli.helpers import (
    async_command,
    echo_error,
    echo_success,
    echo_warning,
    get_db_context,
    setup_db,
    teardown_db,
)

# Map CLI tier names to ApiKeyTier enum values
TIER_MAP = {
    "agent": "agt",
    "dev": "dvl",
    "read": "rdl",
}


def _parse_expires(expires: str | None) -> datetime | None:
    """Parse an expiration duration string like '30d', '12h', '15m'."""
    if expires is None:
        return None
    match = re.match(r"^(\d+)([hdm])$", expires)
    if not match:
        raise click.BadParameter(
            f"Invalid duration: {expires}. Use Nh (hours), Nd (days), or Nm (minutes)."
        )
    amount = int(match.group(1))
    unit = match.group(2)
    delta = {
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "m": timedelta(minutes=amount),
    }[unit]
    return datetime.now(timezone.utc) + delta


@click.group("key")
def keys() -> None:
    """Manage API keys."""


@keys.command("create")
@click.option(
    "--tier",
    "-t",
    type=click.Choice(["agent", "dev", "read"]),
    required=True,
    help="API key tier.",
)
@click.option(
    "--name",
    "-n",
    type=str,
    required=True,
    help="Human-readable name for the key.",
)
@click.option(
    "--expires",
    "-e",
    type=str,
    default=None,
    help="Expiration duration (e.g. 30d, 12h, 15m).",
)
@click.pass_context
@async_command
async def key_create(
    ctx: click.Context, tier: str, name: str, expires: str | None
) -> None:
    """Create a new API key."""
    from grimoire.api.auth import generate_api_key
    from grimoire.db.models import ApiKey, ApiKeyTier

    await setup_db()
    try:
        tier_enum = ApiKeyTier(TIER_MAP[tier])
        raw_key, key_prefix, key_hash = generate_api_key(tier_enum)
        expires_at = _parse_expires(expires)

        async with get_db_context() as db:
            api_key = ApiKey(
                name=name,
                tier=tier_enum,
                key_prefix=key_prefix,
                key_hash=key_hash,
                expires_at=expires_at,
            )
            db.add(api_key)
            await db.commit()
            await db.refresh(api_key)

        echo_success("API key created successfully!")
        click.echo(f"  Key ID:     {api_key.id[:8]}...")
        click.echo(f"  Name:       {name}")
        click.echo(f"  Tier:       {tier}")
        click.echo(f"  Prefix:     {key_prefix}")
        if expires_at:
            click.echo(f"  Expires:    {expires_at.isoformat()}")
        else:
            click.echo("  Expires:    Never")
        click.echo()
        echo_warning("WARNING: This is the only time the full key will be shown:")
        click.echo(f"  {raw_key}")
    finally:
        await teardown_db()


@keys.command("list")
@click.option(
    "--tier",
    "-t",
    type=click.Choice(["agent", "dev", "read"]),
    default=None,
    help="Filter by tier.",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Include revoked keys.",
)
@click.pass_context
@async_command
async def key_list(ctx: click.Context, tier: str | None, show_all: bool) -> None:
    """List API keys."""
    from sqlalchemy import select

    from grimoire.db.models import ApiKey, ApiKeyTier

    await setup_db()
    try:
        async with get_db_context() as db:
            stmt = select(ApiKey).order_by(ApiKey.created_at.desc())
            if tier:
                tier_enum = ApiKeyTier(TIER_MAP[tier])
                stmt = stmt.where(ApiKey.tier == tier_enum)
            if not show_all:
                stmt = stmt.where(ApiKey.revoked_at.is_(None))
            result = await db.execute(stmt)
            api_keys = result.scalars().all()

        if not api_keys:
            click.echo("No API keys found.")
            return

        click.echo(
            f"{'ID':<10}{'PREFIX':<14}{'NAME':<26}{'TIER':<7}{'EXPIRES':<22}{'STATUS'}"
        )
        click.echo(
            f"{'─' * 8:<10}{'─' * 12:<14}{'─' * 24:<26}{'─' * 5:<7}{'─' * 20:<22}{'─' * 8}"
        )
        for k in api_keys:
            status_str = "revoked" if k.revoked_at else "active"
            expires_str = (
                k.expires_at.isoformat()[:19] if k.expires_at else "Never"
            )
            click.echo(
                f"{k.id[:8]:<10}{k.key_prefix:<14}{k.name[:24]:<26}{k.tier.value:<7}{expires_str:<22}{status_str}"
            )
        click.echo(f"\n{len(api_keys)} key(s) found.")
    finally:
        await teardown_db()


@keys.command("revoke")
@click.argument("key_id_or_prefix", type=str)
@click.pass_context
@async_command
async def key_revoke(ctx: click.Context, key_id_or_prefix: str) -> None:
    """Revoke an API key by ID prefix or full key prefix."""
    from sqlalchemy import or_, select

    from grimoire.db.models import ApiKey

    await setup_db()
    try:
        async with get_db_context() as db:
            stmt = select(ApiKey).where(
                or_(
                    ApiKey.id.startswith(key_id_or_prefix),
                    ApiKey.key_prefix == key_id_or_prefix,
                )
            )
            result = await db.execute(stmt)
            api_key = result.scalar_one_or_none()

            if api_key is None:
                echo_error(f"Key not found: {key_id_or_prefix}")
                return

            if api_key.revoked_at:
                echo_error(f"Key {api_key.key_prefix} is already revoked.")
                return

            api_key.revoked_at = datetime.now(timezone.utc)
            await db.commit()

        echo_success(f"Key {api_key.key_prefix} ({api_key.name}) revoked.")
    finally:
        await teardown_db()