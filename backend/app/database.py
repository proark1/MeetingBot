from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"timeout": 15},
    pool_pre_ping=True,
)

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
        await conn.run_sync(Base.metadata.create_all)

        # Enable WAL mode: prevents "database is locked" under concurrent async
        # access and improves read performance alongside writes.
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))

        # Migrate existing tables: add columns introduced after initial creation
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
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists

        # Indexes on hot query columns (idempotent)
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
            await conn.execute(text(stmt))
