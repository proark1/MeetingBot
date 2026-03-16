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
