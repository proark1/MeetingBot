"""Bot lifecycle management — simulates a meeting bot joining, recording, and leaving."""

import asyncio
import logging
import re
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.bot import Bot
from app.services import intelligence_service, webhook_service

logger = logging.getLogger(__name__)

# Platform detection patterns
_PLATFORM_PATTERNS = [
    (r"zoom\.us", "zoom"),
    (r"meet\.google\.com", "google_meet"),
    (r"teams\.microsoft\.com", "microsoft_teams"),
    (r"webex\.com", "webex"),
    (r"whereby\.com", "whereby"),
]


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for pattern, name in _PLATFORM_PATTERNS:
        if re.search(pattern, url_lower):
            return name
    return "unknown"


async def _set_status(db: AsyncSession, bot: Bot, status: str, **kwargs) -> None:
    """Update bot status and fire the corresponding webhook."""
    bot.status = status
    bot.updated_at = datetime.utcnow()
    for key, val in kwargs.items():
        setattr(bot, key, val)
    await db.commit()
    await db.refresh(bot)

    payload = {
        "bot_id": bot.id,
        "status": status,
        "meeting_url": bot.meeting_url,
        "meeting_platform": bot.meeting_platform,
        "ts": datetime.utcnow().isoformat(),
    }
    await webhook_service.dispatch_event(db, f"bot.{status}", payload)


async def run_bot_lifecycle(bot_id: str, db_factory) -> None:
    """
    Background task that simulates the full bot lifecycle:
      ready → joining → in_call → call_ended → done
    """
    async with db_factory() as db:
        from sqlalchemy import select
        result = await db.execute(select(Bot).where(Bot.id == bot_id))
        bot = result.scalar_one_or_none()
        if bot is None:
            logger.error("Bot %s not found — aborting lifecycle", bot_id)
            return

        try:
            # 1. joining
            await _set_status(db, bot, "joining")
            logger.info("Bot %s joining %s", bot_id, bot.meeting_url)
            await asyncio.sleep(3)  # simulate connection delay

            # 2. in_call
            await _set_status(db, bot, "in_call", started_at=datetime.utcnow())
            logger.info("Bot %s in_call", bot_id)

            # Simulate meeting duration
            duration = settings.BOT_SIMULATION_DURATION
            await asyncio.sleep(duration)

            # 3. Generate transcript
            logger.info("Bot %s generating transcript...", bot_id)
            transcript = await intelligence_service.generate_demo_transcript(
                bot.meeting_url
            )
            await _set_status(db, bot, "call_ended", ended_at=datetime.utcnow())

            # Fire transcript_ready
            bot.transcript = transcript
            await db.commit()
            await webhook_service.dispatch_event(
                db,
                "bot.transcript_ready",
                {"bot_id": bot_id, "entry_count": len(transcript)},
            )

            # 4. Analyse with Claude
            logger.info("Bot %s analysing transcript with Claude...", bot_id)
            analysis = await intelligence_service.analyze_transcript(transcript)
            bot.analysis = analysis
            bot.updated_at = datetime.utcnow()
            await db.commit()
            await webhook_service.dispatch_event(
                db,
                "bot.analysis_ready",
                {"bot_id": bot_id},
            )

            # 5. done
            await _set_status(db, bot, "done")
            logger.info("Bot %s done", bot_id)

        except asyncio.CancelledError:
            logger.info("Bot %s lifecycle cancelled", bot_id)
            await _set_status(db, bot, "error", error_message="Lifecycle cancelled")
            raise
        except Exception as exc:
            logger.exception("Bot %s lifecycle error: %s", bot_id, exc)
            await _set_status(
                db, bot, "error", error_message=str(exc)
            )
