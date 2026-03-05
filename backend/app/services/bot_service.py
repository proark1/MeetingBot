"""Bot lifecycle management — drives a real browser bot through a meeting."""

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.bot import Bot
from app.services import intelligence_service, webhook_service
from app.services.browser_bot import run_browser_bot
from app.services.transcription_service import transcribe_audio

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
    """Update bot status, persist, and fire the corresponding webhook + WS event."""
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

    For supported platforms (Google Meet, Zoom, Teams), a real Playwright browser
    bot joins the call, records audio, and transcribes with Whisper.
    For unsupported platforms, falls back to Claude-generated demo transcript.
    """
    async with db_factory() as db:
        result = await db.execute(select(Bot).where(Bot.id == bot_id))
        bot = result.scalar_one_or_none()
        if bot is None:
            logger.error("Bot %s not found — aborting lifecycle", bot_id)
            return

        audio_path = os.path.join(
            tempfile.gettempdir(), f"meetingbot_{bot_id}.wav"
        )
        real_platforms = {"google_meet", "zoom", "microsoft_teams"}
        use_real_bot = bot.meeting_platform in real_platforms

        try:
            # ── 1. joining ─────────────────────────────────────────────────
            await _set_status(db, bot, "joining")
            logger.info(
                "Bot %s → joining %s (%s)", bot_id, bot.meeting_url, bot.meeting_platform
            )

            if use_real_bot:
                # ── 2. Launch browser bot (blocking until meeting ends) ────
                bot_result = await run_browser_bot(
                    meeting_url=bot.meeting_url,
                    platform=bot.meeting_platform,
                    bot_name=bot.bot_name or settings.BOT_NAME_DEFAULT,
                    audio_path=audio_path,
                    admission_timeout=settings.BOT_ADMISSION_TIMEOUT,
                    max_duration=settings.BOT_MAX_DURATION,
                )

                if not bot_result["success"]:
                    raise RuntimeError(bot_result["error"] or "Browser bot failed")

                await _set_status(db, bot, "in_call", started_at=_now())

                # ── 3. Transcribe captured audio ──────────────────────────
                await _set_status(db, bot, "call_ended", ended_at=_now())
                logger.info("Bot %s transcribing audio…", bot_id)

                transcript = await transcribe_audio(
                    audio_path, model_size=settings.WHISPER_MODEL
                )

                if not transcript:
                    logger.warning(
                        "Bot %s: Whisper returned empty transcript — falling back to Claude demo",
                        bot_id,
                    )
                    transcript = await intelligence_service.generate_demo_transcript(
                        bot.meeting_url
                    )
            else:
                # ── Unsupported platform — simulate + Claude demo transcript ──
                logger.info(
                    "Platform '%s' not supported for real bot — using demo transcript",
                    bot.meeting_platform,
                )
                await asyncio.sleep(3)
                await _set_status(db, bot, "in_call", started_at=_now())
                await asyncio.sleep(settings.BOT_SIMULATION_DURATION)
                await _set_status(db, bot, "call_ended", ended_at=_now())

                logger.info("Bot %s generating demo transcript…", bot_id)
                transcript = await intelligence_service.generate_demo_transcript(
                    bot.meeting_url
                )

            # ── 4. Store transcript ───────────────────────────────────────
            bot.transcript = transcript
            bot.updated_at = _now()
            await db.commit()
            await webhook_service.dispatch_event(
                db,
                "bot.transcript_ready",
                {"bot_id": bot_id, "entry_count": len(transcript)},
            )

            # ── 5. Analyse with Claude ────────────────────────────────────
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

            # ── 6. done ───────────────────────────────────────────────────
            await _set_status(db, bot, "done")
            logger.info("Bot %s done", bot_id)

        except asyncio.CancelledError:
            logger.info("Bot %s lifecycle cancelled", bot_id)
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

        finally:
            # Clean up temp audio file
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except Exception:
                pass
