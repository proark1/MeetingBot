"""Standalone database initialisation script.

Run this as a Railway release command (before uvicorn starts) to ensure all
tables exist in the configured PostgreSQL database.  Safe to run on every
deploy — SQLAlchemy's create_all is idempotent (CREATE TABLE IF NOT EXISTS).

Usage:
    python init_db.py
"""

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    from app.config import settings

    url = settings.async_database_url
    masked = url.split("@")[-1] if "@" in url else url
    logger.info("Initialising database: %s", masked)

    from app.db import create_all_tables
    await create_all_tables()
    logger.info("All tables are ready.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.error("Database init failed: %s", exc)
        sys.exit(1)
