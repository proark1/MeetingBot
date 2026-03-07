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

_PLATFORM_PATTERNS = [
    (r"zoom\.us",             "zoom"),
    (r"meet\.google\.com",    "google_meet"),
    (r"teams\.microsoft\.com","microsoft_teams"),
    (r"webex\.com",           "webex"),
    (r"whereby\.com",         "whereby"),
    (r"bluejeans\.com",       "bluejeans"),
    (r"gotomeeting\.com",     "gotomeeting"),
]

_REAL_PLATFORMS = {"google_meet", "zoom", "microsoft_teams"}


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for pattern, name in _PLATFORM_PATTERNS:
        if re.search(pattern, url_lower):
            return name
    return "unknown"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _set_status(db: AsyncSession, bot: Bot, status: str, **kwargs) -> None:
    """Persist status change and fire webhook + WebSocket event."""
    bot.status = status
    bot.updated_at = _now()
    for k, v in kwargs.items():
        setattr(bot, k, v)
    await db.commit()
    await db.refresh(bot)
    await webhook_service.dispatch_event(db, f"bot.{status}", {
        "bot_id":           bot.id,
        "bot_name":         bot.bot_name,
        "status":           status,
        "meeting_url":      bot.meeting_url,
        "meeting_platform": bot.meeting_platform,
        "ts":               _now().isoformat(),
    })


async def _salvage_and_finish(
    db: AsyncSession,
    bot: Bot,
    bot_id: str,
    audio_path: str,
    use_real_bot: bool,
    final_status: str,
    **status_kwargs,
) -> None:
    """Transcribe any captured audio (or generate a demo), run analysis, and
    set the final status.  Called from both the CancelledError and Exception
    handlers so that a transcript is always produced regardless of how the
    lifecycle ended.
    """
    # ── transcript ─────────────────────────────────────────────────────────
    if not bot.transcript:
        transcript: list = []

        if use_real_bot and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            logger.info(
                "Bot %s: transcribing partial audio (%d bytes)",
                bot_id, os.path.getsize(audio_path),
            )
            transcript = await transcribe_audio(audio_path)

        if not transcript:
            logger.info("Bot %s: no captured audio — generating demo transcript", bot_id)
            transcript = await intelligence_service.generate_demo_transcript(bot.meeting_url)

        bot.transcript = transcript
        bot.updated_at = _now()
        await db.commit()
        await webhook_service.dispatch_event(db, "bot.transcript_ready", {
            "bot_id": bot_id, "entry_count": len(transcript),
        })

    # ── analysis ───────────────────────────────────────────────────────────
    if not bot.analysis and bot.transcript:
        analysis = await intelligence_service.analyze_transcript(bot.transcript)
        bot.analysis = analysis
        bot.updated_at = _now()
        await db.commit()
        await webhook_service.dispatch_event(db, "bot.analysis_ready", {"bot_id": bot_id})

    # ── final status ───────────────────────────────────────────────────────
    await _set_status(db, bot, final_status, ended_at=bot.ended_at or _now(), **status_kwargs)


async def run_bot_lifecycle(bot_id: str, db_factory) -> None:
    """
    Full bot lifecycle:
      ready → joining → in_call → call_ended → done

    Real platforms (Google Meet, Zoom, Teams):
      A Playwright browser bot joins the call, records audio, Whisper transcribes.

    Unsupported platforms:
      Simulated lifecycle with a Claude-generated demo transcript.
    """
    async with db_factory() as db:
        result = await db.execute(select(Bot).where(Bot.id == bot_id))
        bot = result.scalar_one_or_none()
        if bot is None:
            logger.error("Bot %s not found", bot_id)
            return

        audio_path   = os.path.join(tempfile.gettempdir(), f"meetingbot_{bot_id}.wav")
        use_real_bot = bot.meeting_platform in _REAL_PLATFORMS

        try:
            # ── 1. joining ────────────────────────────────────────────────
            await _set_status(db, bot, "joining")
            logger.info("Bot %s joining %s (%s)", bot_id, bot.meeting_url, bot.meeting_platform)

            if use_real_bot:
                # on_admitted is called the moment the host admits the bot —
                # we update the status to in_call right then, not after the
                # whole meeting is over.
                async def on_admitted() -> None:
                    await _set_status(db, bot, "in_call", started_at=_now())
                    logger.info("Bot %s is now in_call", bot_id)

                bot_result = await run_browser_bot(
                    meeting_url=bot.meeting_url,
                    platform=bot.meeting_platform,
                    bot_name=bot.bot_name or settings.BOT_NAME_DEFAULT,
                    audio_path=audio_path,
                    admission_timeout=settings.BOT_ADMISSION_TIMEOUT,
                    max_duration=settings.BOT_MAX_DURATION,
                    alone_timeout=settings.BOT_ALONE_TIMEOUT,
                    on_admitted=on_admitted,
                )

                if not bot_result["success"]:
                    raise RuntimeError(bot_result["error"] or "Browser bot failed")

                # ── 2. call_ended → transcribe ────────────────────────────
                await _set_status(db, bot, "call_ended", ended_at=_now())
                logger.info("Bot %s transcribing audio…", bot_id)

                transcript = await transcribe_audio(audio_path)

                if not transcript:
                    logger.warning(
                        "Bot %s: Gemini returned empty transcript — "
                        "audio may not have been captured (check PulseAudio/ffmpeg setup). "
                        "Falling back to demo transcript.",
                        bot_id,
                    )
                    transcript = await intelligence_service.generate_demo_transcript(
                        bot.meeting_url
                    )
            else:
                # ── Unsupported platform — demo mode ──────────────────────
                logger.info(
                    "Platform '%s' not supported for real bot — demo mode",
                    bot.meeting_platform,
                )
                await asyncio.sleep(3)
                await _set_status(db, bot, "in_call", started_at=_now())
                await asyncio.sleep(settings.BOT_SIMULATION_DURATION)
                await _set_status(db, bot, "call_ended", ended_at=_now())
                transcript = await intelligence_service.generate_demo_transcript(
                    bot.meeting_url
                )

            # ── 3. Store transcript ───────────────────────────────────────
            bot.transcript = transcript
            bot.updated_at = _now()
            await db.commit()
            await webhook_service.dispatch_event(db, "bot.transcript_ready", {
                "bot_id": bot_id, "entry_count": len(transcript),
            })

            # ── 4. Analyse with Claude ────────────────────────────────────
            logger.info("Bot %s analysing transcript…", bot_id)
            analysis = await intelligence_service.analyze_transcript(transcript)
            bot.analysis = analysis
            bot.updated_at = _now()
            await db.commit()
            await webhook_service.dispatch_event(db, "bot.analysis_ready", {"bot_id": bot_id})

            # ── 5. done ───────────────────────────────────────────────────
            await _set_status(db, bot, "done")
            logger.info("Bot %s done", bot_id)

        except asyncio.CancelledError:
            logger.info("Bot %s cancelled — salvaging transcript", bot_id)
            try:
                await _salvage_and_finish(
                    db, bot, bot_id, audio_path, use_real_bot, "cancelled"
                )
            except Exception:
                logger.exception("Bot %s: error during cancellation cleanup", bot_id)
                try:
                    await _set_status(db, bot, "cancelled", ended_at=bot.ended_at or _now())
                except Exception:
                    pass
            # Do NOT re-raise: let the task finish cleanly so asyncio.shield()
            # in delete_bot can observe completion rather than propagating cancel.

        except Exception as exc:
            logger.exception("Bot %s error: %s", bot_id, exc)
            try:
                await _salvage_and_finish(
                    db, bot, bot_id, audio_path, use_real_bot,
                    "error", error_message=str(exc),
                )
            except Exception:
                logger.exception("Bot %s: error during error cleanup", bot_id)
                try:
                    await _set_status(db, bot, "error", error_message=str(exc))
                except Exception:
                    pass

        finally:
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except Exception:
                pass
