"""Alembic migration environment.

Targets the same database as the app (URL pulled from app Settings, not the ini)
and autogenerates against ``app.db.Base.metadata``. Supports both sync and async
drivers — async engines (aiosqlite/asyncpg) are run via ``run_sync``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from alembic import context

# Import the app's metadata + URL. Importing app.models.account ensures every
# ORM table is registered on Base.metadata before autogenerate runs.
from app.config import settings
from app.db import Base
import app.models.account  # noqa: F401  (registers models on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    """The async DB URL from app settings (single source of truth)."""
    return settings.async_database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite ALTER support
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_db_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
