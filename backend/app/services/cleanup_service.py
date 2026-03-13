"""Recording retention — auto-delete old WAV files to prevent unbounded disk usage.

Triggered daily at 03:00 UTC by APScheduler (registered in main.py).
Controlled by RECORDING_RETENTION_DAYS (default 30; 0 = disabled / keep forever).
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import defer

from app.models.bot import Bot

logger = logging.getLogger(__name__)


async def purge_old_recordings(db_factory) -> None:
    """Delete WAV recordings for bots whose meeting ended more than RECORDING_RETENTION_DAYS ago."""
    from app.config import settings

    retention_days = settings.RECORDING_RETENTION_DAYS
    if retention_days <= 0:
        return  # retention disabled

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    async with db_factory() as db:
        # Select only the columns we need — avoid loading transcript/analysis
        bots = (
            await db.execute(
                select(Bot)
                .options(
                    defer(Bot.transcript),
                    defer(Bot.analysis),
                    defer(Bot.chapters),
                    defer(Bot.speaker_stats),
                    defer(Bot.vocabulary),
                )
                .where(Bot.recording_path.isnot(None))
                .where(Bot.ended_at < cutoff)
            )
        ).scalars().all()

        if not bots:
            logger.debug("Recording cleanup: no expired recordings found")
            return

        deleted = 0
        missing = 0
        for bot in bots:
            path = bot.recording_path
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    deleted += 1
                except OSError as exc:
                    logger.warning("Could not delete recording %s: %s", path, exc)
                    continue
            else:
                missing += 1  # file already gone, just clear the DB reference

            bot.recording_path = None

        await db.commit()

    logger.info(
        "Recording cleanup complete: %d file(s) deleted, %d already missing "
        "(retention=%d days, cutoff=%s)",
        deleted, missing, retention_days, cutoff.strftime("%Y-%m-%d"),
    )
