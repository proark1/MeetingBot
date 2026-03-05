"""Bot lifecycle management — simulates a meeting bot joining, recording, and leaving."""

import asyncio
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
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
    (r"bluejeans\.com", "bluejeans"),
    (r"gotomeeting\.com", "gotomeeting"),
]


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for pattern, name in _PLATFORM_PATTERNS:
        if re.search(pattern, url_lower):
            return name
    return "unknown"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _set_status(db: AsyncSession, bot: Bot, status: str, **kwargs) -> None:
    """Update bot status, persist, and fire the corresponding event (webhook + WS)."""
    bot.status = status
    bot.updated_at = _now()
    for key, val in kwargs.items():
        setattr(bot, key, val)
    await db.commit()
    await db.refresh(bot)

    payload = {
        "bot_id": bot.id,
        "bot_name": bot.bot_name,
        "status": status,
        "meeting_url": bot.meeting_url,
        "meeting_platform": bot.meeting_platform,
        "ts": _now().isoformat(),
    }
    await webhook_service.dispatch_event(db, f"bot.{status}", payload)


async def run_bot_lifecycle(bot_id: str, db_factory) -> None:
    """
    Background task that drives the full bot lifecycle:
      ready → joining → in_call → call_ended → done
    """
    async with db_factory() as db:
        result = await db.execute(select(Bot).where(Bot.id == bot_id))
        bot = result.scalar_one_or_none()
        if bot is None:
            logger.error("Bot %s not found — aborting lifecycle", bot_id)
            return

        try:
            # 1. joining
            await _set_status(db, bot, "joining")
            logger.info("Bot %s joining %s", bot_id, bot.meeting_url)
            await asyncio.sleep(3)

            # 2. in_call
            await _set_status(db, bot, "in_call", started_at=_now())
            logger.info("Bot %s in_call  duration=%ds", bot_id, settings.BOT_SIMULATION_DURATION)
            await asyncio.sleep(settings.BOT_SIMULATION_DURATION)

            # 3. Generate transcript (Claude or fallback)
            logger.info("Bot %s generating transcript…", bot_id)
            transcript = await intelligence_service.generate_demo_transcript(bot.meeting_url)
            await _set_status(db, bot, "call_ended", ended_at=_now())

            bot.transcript = transcript
            bot.updated_at = _now()
            await db.commit()
            await webhook_service.dispatch_event(
                db,
                "bot.transcript_ready",
                {"bot_id": bot_id, "entry_count": len(transcript)},
            )

            # 4. Analyse with Claude
            logger.info("Bot %s analysing transcript…", bot_id)
            analysis = await intelligence_service.analyze_transcript(transcript)
            bot.analysis = analysis
            bot.updated_at = _now()
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
            logger.info("Bot %s lifecycle cancelled by caller", bot_id)
            # Gracefully mark as ended rather than error
            try:
                await _set_status(db, bot, "call_ended", ended_at=_now())
            except Exception:
                pass
            raise
        except Exception as exc:
            logger.exception("Bot %s lifecycle error: %s", bot_id, exc)
            try:
                await _set_status(db, bot, "error", error_message=str(exc))
            except Exception:
                pass
