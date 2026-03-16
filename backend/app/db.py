"""Async SQLAlchemy database setup."""

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

def _engine_kwargs() -> dict:
    """Return extra kwargs for create_async_engine based on the configured DB."""
    url = settings.async_database_url
    if "postgresql" in url:
        # asyncpg requires ssl=True when connecting over a public/TLS endpoint.
        # Railway's private-network URL works with ssl=False; the public URL needs ssl=True.
        # We default to ssl=False (private network) but allow override via DATABASE_URL
        # query string — e.g. append ?ssl=require to DATABASE_URL for external clients.
        if "ssl=require" in url or "sslmode=require" in url:
            return {"connect_args": {"ssl": True}}
        return {}
    return {}


engine = create_async_engine(settings.async_database_url, echo=False, **_engine_kwargs())
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def create_all_tables() -> None:
    from app.models import account  # noqa: F401 — registers models with Base.metadata
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_schema)


def _migrate_schema(conn) -> None:
    """Apply additive schema migrations for columns added after initial deployment."""
    from sqlalchemy import inspect, text

    inspector = inspect(conn)

    # accounts table — columns added in v2.x
    if "accounts" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("accounts")}
        migrations = [
            ("is_admin", "BOOLEAN NOT NULL DEFAULT 0"),
            ("account_type", "VARCHAR(20) NOT NULL DEFAULT 'personal'"),
            ("wallet_address", "VARCHAR(42)"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing:
                conn.execute(text(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_def}"))

    # api_keys table — last_used_at added in v2.x
    if "api_keys" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("api_keys")}
        if "last_used_at" not in existing:
            conn.execute(text("ALTER TABLE api_keys ADD COLUMN last_used_at DATETIME"))

    # bot_snapshots — sub_user_id added in v2.1
    if "bot_snapshots" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("bot_snapshots")}
        if "sub_user_id" not in existing:
            conn.execute(text("ALTER TABLE bot_snapshots ADD COLUMN sub_user_id VARCHAR(255)"))

    # accounts table — columns added in v3.x (plans, notifications, usage counters)
    if "accounts" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("accounts")}
        v3_migrations = [
            ("plan",                 "VARCHAR(20) NOT NULL DEFAULT 'free'"),
            ("monthly_bots_used",   "INTEGER NOT NULL DEFAULT 0"),
            ("monthly_reset_at",    "DATETIME"),
            ("notify_on_done",      "BOOLEAN NOT NULL DEFAULT 1"),
            ("notify_email",        "VARCHAR(255)"),
        ]
        for col_name, col_def in v3_migrations:
            if col_name not in existing:
                conn.execute(text(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_def}"))

    # webhooks table — v4.x: add account_id for ownership scoping
    if "webhooks" in inspector.get_table_names():
        existing = {col["name"] for col in inspector.get_columns("webhooks")}
        if "account_id" not in existing:
            conn.execute(text("ALTER TABLE webhooks ADD COLUMN account_id VARCHAR(36)"))
