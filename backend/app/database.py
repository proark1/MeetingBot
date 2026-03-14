import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)

# Normalise the DATABASE_URL for the async driver.
# Railway (and most PaaS) provide "postgresql://" or the legacy "postgres://"
# alias, but asyncpg requires the "postgresql+asyncpg://" scheme.
_db_url = settings.DATABASE_URL
if _db_url.startswith("postgres://"):
    _db_url = "postgresql+asyncpg://" + _db_url[len("postgres://"):]
elif _db_url.startswith("postgresql://"):
    _db_url = "postgresql+asyncpg://" + _db_url[len("postgresql://"):]

_is_sqlite = _db_url.startswith("sqlite")

_engine_kwargs: dict = {"echo": False, "pool_pre_ping": True}

if _is_sqlite:
    # SQLite needs a per-connection timeout; server-pool args are not applicable.
    _engine_kwargs["connect_args"] = {"timeout": 30}
else:
    # Pool sizing only applies to server-based databases (PostgreSQL, MySQL, …).
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 20
    _engine_kwargs["pool_timeout"] = 30

engine = create_async_engine(_db_url, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    async with engine.begin() as conn:
        from app.models import bot, webhook, highlight, action_item, template  # noqa: F401 — registers models
        from app.models import speaker_profile  # noqa: F401 — registers SpeakerProfile
        await conn.run_sync(Base.metadata.create_all)

        if _is_sqlite:
            # Enable WAL mode: prevents "database is locked" under concurrent async
            # access and improves read performance alongside writes.
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))

        if _is_sqlite:
            # SQLite-only: migrate existing tables by adding columns that were
            # introduced after initial schema creation.  On PostgreSQL, SQLAlchemy's
            # create_all() handles the full schema on first run, so these are unnecessary.
            for stmt in [
                "ALTER TABLE bots ADD COLUMN participants JSON DEFAULT '[]'",
                "ALTER TABLE webhooks ADD COLUMN consecutive_failures INTEGER DEFAULT 0",
                "ALTER TABLE bots ADD COLUMN chapters JSON",
                "ALTER TABLE bots ADD COLUMN speaker_stats JSON",
                "ALTER TABLE bots ADD COLUMN recording_path TEXT",
                "ALTER TABLE bots ADD COLUMN share_token TEXT",
                "ALTER TABLE bots ADD COLUMN notify_email TEXT",
                "ALTER TABLE bots ADD COLUMN template_id TEXT",
                "ALTER TABLE bots ADD COLUMN vocabulary JSON",
                "ALTER TABLE bots ADD COLUMN analysis_mode TEXT DEFAULT 'full'",
                "ALTER TABLE bots ADD COLUMN respond_on_mention INTEGER DEFAULT 1",
                "ALTER TABLE bots ADD COLUMN mention_response_mode TEXT DEFAULT 'text'",
                "ALTER TABLE bots ADD COLUMN tts_provider TEXT DEFAULT 'edge'",
                "ALTER TABLE bots ADD COLUMN start_muted INTEGER DEFAULT 0",
                "ALTER TABLE bots ADD COLUMN live_transcription INTEGER DEFAULT 0",
                "ALTER TABLE bots ADD COLUMN prompt_override TEXT",
                "ALTER TABLE bots ADD COLUMN ai_usage JSON DEFAULT '[]'",
                "ALTER TABLE bots ADD COLUMN ai_total_tokens INTEGER DEFAULT 0",
                "ALTER TABLE bots ADD COLUMN ai_total_cost_usd REAL DEFAULT 0.0",
                "ALTER TABLE bots ADD COLUMN ai_primary_model TEXT",
                "ALTER TABLE bots ADD COLUMN meeting_duration_s REAL DEFAULT 0.0",
            ]:
                try:
                    await conn.execute(text(stmt))
                except Exception as mig_exc:
                    # Most failures are benign "column already exists" — log at DEBUG
                    # so genuine schema errors (typos, wrong table names) are visible.
                    logger.debug("Migration skipped (already applied?): %s — %s", stmt, mig_exc)

        # speaker_profiles table is created by SQLAlchemy metadata above;
        # no ALTER TABLE needed for it since it's new.

        # Indexes on hot query columns — use IF NOT EXISTS (supported by both SQLite
        # and PostgreSQL) so this is safe to run on every startup.
        for stmt in [
            "CREATE INDEX IF NOT EXISTS ix_bot_status      ON bots (status)",
            "CREATE INDEX IF NOT EXISTS ix_bot_created_at  ON bots (created_at)",
            "CREATE INDEX IF NOT EXISTS ix_bot_meeting_url ON bots (meeting_url)",
            # share_token added via ALTER TABLE which doesn't carry the ORM unique
            # constraint — create the unique index explicitly so existing databases
            # also enforce it (NULL values are excluded so un-shared bots don't clash).
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_bot_share_token ON bots (share_token) WHERE share_token IS NOT NULL",
            # Foreign-key indexes so queries filtering by bot_id don't full-scan
            "CREATE INDEX IF NOT EXISTS ix_action_item_bot_id ON action_items (bot_id)",
            "CREATE INDEX IF NOT EXISTS ix_highlight_bot_id    ON highlights   (bot_id)",
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception as idx_exc:
                logger.debug("Index creation skipped: %s", idx_exc)
