"""Standalone database initialisation script.

Run this as a Railway release command (before uvicorn starts) to ensure all
tables exist in the configured PostgreSQL database.  Safe to run on every
deploy — SQLAlchemy's create_all is idempotent (CREATE TABLE IF NOT EXISTS).

Retries up to 5 times with 5 s delay to handle slow-starting databases
(common in Railway where the DB and app containers boot in parallel).

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

MAX_ATTEMPTS = 5
RETRY_DELAY_S = 5


async def main() -> None:
    from app.config import settings

    url = settings.async_database_url
    masked = url.split("@")[-1] if "@" in url else url
    logger.info("Initialising database: %s", masked)

    from app.db import create_all_tables

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            await asyncio.wait_for(create_all_tables(), timeout=30.0)
            logger.info("All tables are ready.")
            return
        except asyncio.TimeoutError:
            logger.warning("DB init attempt %d/%d timed out after 30 s", attempt, MAX_ATTEMPTS)
        except Exception as exc:
            logger.warning("DB init attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, exc)
        if attempt < MAX_ATTEMPTS:
            logger.info("Retrying in %d s…", RETRY_DELAY_S)
            await asyncio.sleep(RETRY_DELAY_S)

    raise RuntimeError(f"Database initialization failed after {MAX_ATTEMPTS} attempts")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.error("Database init failed: %s", exc)
        sys.exit(1)
