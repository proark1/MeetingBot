from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
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
        from app.models import bot, webhook  # noqa: F401 — registers models
        await conn.run_sync(Base.metadata.create_all)

        # Enable WAL mode: prevents "database is locked" under concurrent async
        # access and improves read performance alongside writes.
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))

        # Migrate existing tables: add columns introduced after initial creation
        for stmt in [
            "ALTER TABLE bots ADD COLUMN participants JSON DEFAULT '[]'",
        ]:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass  # column already exists
