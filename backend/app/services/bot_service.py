"""Bot lifecycle management — drives a real browser bot through a meeting."""

import asyncio
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# Persistent recordings directory — created at module import time
_RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/app/data/recordings"))
try:
    _RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    _RECORDINGS_DIR = Path(tempfile.gettempdir()) / "meetingbot_recordings"
    _RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.action_item import ActionItem
from app.models.bot import Bot
from app.api.ws import manager as ws_manager
from app.services import intelligence_service, webhook_service
from app.services.browser_bot import run_browser_bot
from app.services.transcription_service import transcribe_audio

logger = logging.getLogger(__name__)


import re as _re
_FILLER_WORDS = _re.compile(r'\b(um|uh|like|you know|so|basically|literally|actually|right)\b', _re.IGNORECASE)


def _compute_speaker_stats(transcript: list[dict]) -> list[dict]:
    """Compute per-speaker talk-time + conversation intelligence from transcript."""
    if not transcript:
        return []
    entries = sorted(transcript, key=lambda e: e.get("timestamp", 0))

    speaker_time: dict[str, float] = {}
    speaker_questions: dict[str, int] = {}
    speaker_fillers: dict[str, int] = {}
    speaker_monologue: dict[str, float] = {}  # longest single turn in seconds
    speaker_turns: dict[str, int] = {}

    for i, e in enumerate(entries):
        speaker = e.get("speaker", "Unknown")
        text = e.get("text", "")

        # Duration = gap to next entry, capped at 60 s
        if i + 1 < len(entries):
            duration = entries[i + 1].get("timestamp", 0) - e.get("timestamp", 0)
        else:
            duration = 5.0
        duration = min(max(duration, 0.0), 60.0)

        speaker_time[speaker] = speaker_time.get(speaker, 0.0) + duration
        speaker_turns[speaker] = speaker_turns.get(speaker, 0) + 1
        speaker_questions[speaker] = speaker_questions.get(speaker, 0) + text.count("?")
        speaker_fillers[speaker] = speaker_fillers.get(speaker, 0) + len(_FILLER_WORDS.findall(text))
        speaker_monologue[speaker] = max(speaker_monologue.get(speaker, 0.0), duration)

    total = sum(speaker_time.values())
    if total == 0:
        return []
    return [
        {
            "name": name,
            "talk_time_s": round(t, 1),
            "talk_pct": round(t / total * 100, 1),
            "turns": speaker_turns.get(name, 0),
            "questions": speaker_questions.get(name, 0),
            "filler_words": speaker_fillers.get(name, 0),
            "longest_monologue_s": round(speaker_monologue.get(name, 0.0), 1),
        }
        for name, t in sorted(speaker_time.items(), key=lambda x: x[1], reverse=True)
    ]


def _unwrap_safelinks(url: str) -> str:
    """Extract the real URL from a Microsoft SafeLinks wrapper URL."""
    try:
        parsed = urlparse(url)
        if "safelinks.protection.outlook.com" in parsed.netloc:
            qs = parse_qs(parsed.query)
            if "url" in qs:
                return unquote(qs["url"][0])
    except Exception:
        pass
    return url


_PLATFORM_NETLOC: dict[str, set[str]] = {
    "zoom":              {"zoom.us", "zoom.com"},
    "google_meet":       {"meet.google.com"},
    "microsoft_teams":   {"teams.microsoft.com", "teams.live.com"},
    "webex":             {"webex.com", "cisco.webex.com"},
    "whereby":           {"whereby.com"},
    "bluejeans":         {"bluejeans.com"},
    "gotomeeting":       {"gotomeeting.com"},
}

_REAL_PLATFORMS = {"google_meet", "zoom", "microsoft_teams"}


def detect_platform(url: str) -> str:
    """Return platform key by matching the parsed netloc — prevents subdomain spoofing."""
    try:
        url = _unwrap_safelinks(url)
        netloc = urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return "unknown"
    for platform, hosts in _PLATFORM_NETLOC.items():
        if any(netloc == h or netloc.endswith("." + h) for h in hosts):
            return platform
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
            logger.warning(
                "Bot %s: no usable audio captured — transcript will be empty",
                bot_id,
            )
            bot.error_message = (
                (bot.error_message or "") +
                " No audio was captured or transcription returned no content."
            )

        bot.transcript = transcript
        bot.updated_at = _now()
        await db.commit()
        await webhook_service.dispatch_event(db, "bot.transcript_ready", {
            "bot_id": bot_id, "entry_count": len(transcript),
        })

    # ── analysis + chapters + speaker stats ────────────────────────────────
    analysis_mode = getattr(bot, "analysis_mode", "full") or "full"
    if not bot.analysis and bot.transcript and analysis_mode != "transcript_only":
        # Run analysis and chapter generation in parallel.
        analysis_result, chapters_result = await asyncio.gather(
            intelligence_service.analyze_transcript(bot.transcript),
            intelligence_service.generate_chapters(bot.transcript),
            return_exceptions=True,
        )

        if isinstance(analysis_result, Exception):
            logger.error("Analysis failed for bot %s: %s", bot_id, analysis_result)
            bot.analysis = {
                "summary": "Analysis unavailable — an error occurred during processing.",
                "key_points": [], "action_items": [], "decisions": [],
                "next_steps": [], "sentiment": "neutral", "topics": [],
            }
            bot.error_message = (bot.error_message or "") + f" [analysis error: {analysis_result}]"
        else:
            bot.analysis = analysis_result

        bot.speaker_stats = _compute_speaker_stats(bot.transcript)

        if isinstance(chapters_result, Exception):
            logger.warning("Chapter generation failed for bot %s: %s", bot_id, chapters_result)
            bot.chapters = []
        else:
            bot.chapters = chapters_result or []

        if use_real_bot and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            bot.recording_path = audio_path

        bot.updated_at = _now()
        await db.commit()
        await webhook_service.dispatch_event(db, "bot.analysis_ready", {"bot_id": bot_id})
    elif analysis_mode == "transcript_only":
        logger.info("Bot %s analysis_mode=transcript_only — skipping AI analysis", bot_id)
        # Speaker stats are fast/local and independent of AI analysis — always compute them.
        bot.speaker_stats = _compute_speaker_stats(bot.transcript)
        if use_real_bot and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            bot.recording_path = audio_path
        bot.updated_at = _now()
        await db.commit()

    # ── final status ───────────────────────────────────────────────────────
    await _set_status(db, bot, final_status, ended_at=bot.ended_at or _now(), **status_kwargs)


async def run_bot_lifecycle(bot_id: str, db_factory) -> None:
    """
    Full bot lifecycle:
      (scheduled →) joining → in_call → call_ended → done

    Real platforms (Google Meet, Zoom, Teams):
      A Playwright browser bot joins the call, records audio, Gemini transcribes.

    Unsupported platforms:
      Simulated lifecycle with a Gemini-generated demo transcript.
    """
    async with db_factory() as db:
        result = await db.execute(select(Bot).where(Bot.id == bot_id))
        bot = result.scalar_one_or_none()
        if bot is None:
            logger.error("Bot %s not found", bot_id)
            return

        audio_path   = str(_RECORDINGS_DIR / f"{bot_id}.wav")
        use_real_bot = bot.meeting_platform in _REAL_PLATFORMS

        try:
            # ── 0. scheduled — wait until join_at ─────────────────────────
            if bot.join_at:
                delay = (bot.join_at.replace(tzinfo=timezone.utc) - _now()).total_seconds()
                if delay > 86400:
                    logger.warning(
                        "Bot %s join_at is more than 24 h away (%.0f s) — starting immediately",
                        bot_id, delay,
                    )
                elif delay > 0:
                    logger.info("Bot %s scheduled — waiting %.0f s until join_at", bot_id, delay)
                    await asyncio.sleep(delay)

            # ── 1. joining ────────────────────────────────────────────────
            await _set_status(db, bot, "joining")
            logger.info("Bot %s joining %s (%s)", bot_id, bot.meeting_url, bot.meeting_platform)

            if use_real_bot:
                # on_admitted is called the moment the host admits the bot —
                # we update the status to in_call right then, not after the
                # whole meeting is over.
                admitted = False

                async def on_admitted() -> None:
                    nonlocal admitted
                    admitted = True
                    await _set_status(db, bot, "in_call", started_at=_now())
                    logger.info("Bot %s is now in_call", bot_id)

                # ── Live transcript DB-save callback ──────────────────────
                # Called by _streaming_transcription_loop for each utterance.
                # Flushes to DB every 10 entries or every 30 s, whichever
                # comes first, so the transcript builds up in real-time.
                _live_buffer: list = []
                _last_flush: float = time.monotonic()
                _live_lock = asyncio.Lock()

                async def on_live_entry(entry: dict) -> None:
                    nonlocal _last_flush
                    # Lock prevents a race between append and the list copy
                    # during flush when both coroutines run on the same loop.
                    async with _live_lock:
                        _live_buffer.append(entry)
                        should_flush = (
                            len(_live_buffer) >= 10
                            or time.monotonic() - _last_flush >= 30
                        )
                    # Push entry immediately to connected WebSocket clients for live display
                    await ws_manager.broadcast(
                        "bot.live_transcript",
                        {"bot_id": bot_id, "entry": entry},
                    )
                    if should_flush:
                        async with _live_lock:
                            snapshot = list(_live_buffer)
                        bot.transcript = snapshot
                        bot.updated_at = _now()
                        try:
                            await db.commit()
                        except Exception as flush_exc:
                            logger.warning("Live transcript flush error: %s", flush_exc)
                            await db.rollback()
                        _last_flush = time.monotonic()

                max_retries = settings.BOT_JOIN_MAX_RETRIES
                retry_delay = settings.BOT_JOIN_RETRY_DELAY_S
                last_error: str = ""

                for attempt in range(max_retries + 1):
                    if attempt > 0:
                        logger.info(
                            "Bot %s join attempt %d/%d (retrying in %d s)…",
                            bot_id, attempt + 1, max_retries + 1, retry_delay,
                        )
                        await asyncio.sleep(retry_delay)
                        admitted = False  # reset for fresh attempt
                        _live_buffer.clear()

                    bot_result = await run_browser_bot(
                        meeting_url=bot.meeting_url,
                        platform=bot.meeting_platform,
                        bot_name=bot.bot_name or settings.BOT_NAME_DEFAULT,
                        audio_path=audio_path,
                        admission_timeout=settings.BOT_ADMISSION_TIMEOUT,
                        max_duration=settings.BOT_MAX_DURATION,
                        alone_timeout=settings.BOT_ALONE_TIMEOUT,
                        on_admitted=on_admitted,
                        respond_on_mention=getattr(bot, "respond_on_mention", True),
                        mention_response_mode=getattr(bot, "mention_response_mode", "text"),
                        tts_provider=getattr(bot, "tts_provider", "edge"),
                        start_muted=getattr(bot, "start_muted", False),
                        live_transcription=getattr(bot, "live_transcription", False),
                        on_live_transcript_entry=on_live_entry,
                        gemini_api_key=settings.GEMINI_API_KEY or "",
                    )

                    if bot_result["success"]:
                        break

                    last_error = bot_result["error"] or "Browser bot failed"
                    # Don't retry if the bot was admitted — the failure happened
                    # during the call, not during the join phase.
                    if admitted:
                        break
                    if attempt < max_retries:
                        logger.warning("Bot %s join failed (attempt %d): %s", bot_id, attempt + 1, last_error)
                    else:
                        raise RuntimeError(last_error)

                if not bot_result["success"] and not admitted:
                    raise RuntimeError(last_error or "Browser bot failed after all retries")

                # ── 2. call_ended → transcribe ────────────────────────────
                scraped_participants: list[str] = bot_result.get("participants") or []
                live_transcript_entries: list = bot_result.get("live_transcript") or []

                # Final flush of live transcript buffer so no entries are lost
                # before the batch transcription overwrites bot.transcript.
                async with _live_lock:
                    final_buffer = list(_live_buffer)
                if final_buffer:
                    bot.transcript = final_buffer
                    bot.updated_at = _now()
                    try:
                        await db.commit()
                    except Exception as flush_exc:
                        logger.warning("Final live transcript flush error: %s", flush_exc)
                        await db.rollback()
                    logger.info(
                        "Bot %s: flushed %d live transcript entries before batch processing",
                        bot_id, len(_live_buffer),
                    )

                await _set_status(db, bot, "call_ended", ended_at=_now())
                logger.info("Bot %s transcribing audio…", bot_id)

                transcript = await transcribe_audio(audio_path, known_participants=scraped_participants)

                # Merge strategy: the batch transcript has speaker labels and
                # accurate timestamps, but the live streaming entries capture
                # speech detected in real-time (which the batch may miss if
                # audio quality differs or the model behaves differently).
                #
                # Use batch as primary when it's rich, but always supplement
                # with live entries that aren't already covered by the batch.
                if live_transcript_entries:
                    if len(transcript) < 3:
                        logger.info(
                            "Bot %s: batch transcript sparse (%d entries) — "
                            "using %d live streaming entries instead",
                            bot_id, len(transcript), len(live_transcript_entries),
                        )
                        transcript = live_transcript_entries
                    else:
                        # Supplement batch with any live entries whose text
                        # doesn't already appear in the batch transcript.
                        batch_texts = {e.get("text", "").strip().lower() for e in transcript}
                        extra = [
                            e for e in live_transcript_entries
                            if e.get("text", "").strip().lower() not in batch_texts
                        ]
                        if extra:
                            logger.info(
                                "Bot %s: batch transcript has %d entries, "
                                "supplementing with %d unique live entries (out of %d total)",
                                bot_id, len(transcript), len(extra), len(live_transcript_entries),
                            )
                            # Insert live-only entries at appropriate positions by timestamp
                            transcript = sorted(
                                transcript + extra,
                                key=lambda e: e.get("timestamp", 0),
                            )
                        else:
                            logger.info(
                                "Bot %s: batch transcript has %d entries, "
                                "live streaming produced %d entries (all covered by batch)",
                                bot_id, len(transcript), len(live_transcript_entries),
                            )

                if not transcript:
                    logger.warning(
                        "Bot %s: Gemini returned empty transcript — "
                        "audio may not have been captured (check PulseAudio/ffmpeg setup).",
                        bot_id,
                    )
                    bot.error_message = (
                        (bot.error_message or "") +
                        " Transcription returned no content — the audio may be silent or too short."
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
                if not transcript:
                    logger.warning(
                        "Bot %s: demo transcript generation returned empty result", bot_id
                    )
                    bot.error_message = (
                        (bot.error_message or "") +
                        " Demo transcript generation returned no content."
                    )
                bot.extra_metadata = {**(bot.extra_metadata or {}), "is_demo_transcript": True}

            # ── 3. Store transcript + participants ────────────────────────
            bot.transcript = transcript
            # Derive participants: prefer scraped names, fall back to transcript speakers.
            # Dedup case-insensitively so "Alice" and "alice" count as one person.
            raw_names: list[str] = list(scraped_participants or []) if use_real_bot else []
            for e in transcript:
                name = e.get("speaker", "")
                if name:
                    raw_names.append(name)
            seen_lower: set[str] = set()
            unique_names: list[str] = []
            for name in raw_names:
                key = name.strip().lower()
                if key and key not in seen_lower:
                    seen_lower.add(key)
                    unique_names.append(name.strip())
            bot.participants = sorted(unique_names)
            bot.updated_at = _now()
            await db.commit()
            await webhook_service.dispatch_event(db, "bot.transcript_ready", {
                "bot_id": bot_id, "entry_count": len(transcript),
            })

            # ── 4. Analyse + chapters + speaker stats ─────────────────────
            # Speaker stats are always computed (fast, local, no AI needed).
            bot.speaker_stats = _compute_speaker_stats(transcript)

            # Persist recording path if audio was captured (regardless of mode).
            if use_real_bot and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                bot.recording_path = audio_path

            analysis_mode = getattr(bot, "analysis_mode", "full") or "full"

            if analysis_mode == "transcript_only":
                # Skip all AI processing — deliver only the raw transcript.
                logger.info("Bot %s analysis_mode=transcript_only — skipping AI analysis", bot_id)
                bot.analysis = None
                bot.chapters = []
                bot.updated_at = _now()
                await db.commit()

            else:
                # ── full mode: AI analysis + chapters + action items ───────
                logger.info("Bot %s analysing transcript…", bot_id)
                prompt_override = None
                # Inline prompt (used by the "Customized" seed template)
                if bot.prompt_override:
                    prompt_override = bot.prompt_override
                elif bot.template_id:
                    try:
                        from app.models.template import MeetingTemplate
                        tmpl_row = (await db.execute(
                            select(MeetingTemplate).where(MeetingTemplate.id == bot.template_id)
                        )).scalar_one_or_none()
                        if tmpl_row and tmpl_row.prompt_override:
                            prompt_override = tmpl_row.prompt_override
                    except Exception as exc:
                        logger.warning("Template lookup failed for bot %s: %s", bot_id, exc)
                # Run analysis and chapter generation in parallel — saves 2–5 s per meeting.
                analysis_result, chapters_result = await asyncio.gather(
                    intelligence_service.analyze_transcript(
                        transcript,
                        prompt_override=prompt_override,
                        vocabulary=bot.vocabulary or [],
                    ),
                    intelligence_service.generate_chapters(transcript),
                    return_exceptions=True,
                )

                if isinstance(analysis_result, Exception):
                    logger.error("Analysis failed for bot %s: %s", bot_id, analysis_result)
                    bot.analysis = {
                        "summary": "Analysis unavailable — an error occurred during processing.",
                        "key_points": [], "action_items": [], "decisions": [],
                        "next_steps": [], "sentiment": "neutral", "topics": [],
                    }
                    bot.error_message = (bot.error_message or "") + f" [analysis error: {analysis_result}]"
                else:
                    bot.analysis = analysis_result

                if isinstance(chapters_result, Exception):
                    logger.warning("Chapter generation failed for bot %s: %s", bot_id, chapters_result)
                    bot.chapters = []
                else:
                    bot.chapters = chapters_result or []

                bot.updated_at = _now()
                await db.commit()
                await webhook_service.dispatch_event(db, "bot.analysis_ready", {"bot_id": bot_id})

                # ── 5. Persist action items to DB ──────────────────────────
                if bot.analysis:
                    await _persist_action_items(db, bot)

                # ── 6. Post-meeting notifications (run in parallel) ────────
                _notification_coros = []

                if bot.notify_email:
                    async def _send_email(_bot=bot):
                        from app.services import email_service
                        await email_service.send_meeting_summary(_bot)
                    _notification_coros.append(_send_email())

                if settings.SLACK_WEBHOOK_URL or (bot.extra_metadata or {}).get("slack_webhook_url"):
                    _slack_url = (bot.extra_metadata or {}).get("slack_webhook_url") or settings.SLACK_WEBHOOK_URL
                    async def _send_slack(_bot=bot, _url=_slack_url):
                        from app.services import slack_service
                        await slack_service.send_meeting_summary(_bot, _url)
                    _notification_coros.append(_send_slack())

                if settings.NOTION_API_KEY and settings.NOTION_DATABASE_ID:
                    async def _send_notion(_bot=bot):
                        from app.services import notion_service
                        await notion_service.push_meeting(_bot)
                    _notification_coros.append(_send_notion())

                if settings.LINEAR_API_KEY and settings.LINEAR_TEAM_ID:
                    async def _send_linear(_bot=bot):
                        from app.services import linear_service
                        await linear_service.push_action_items(_bot)
                    _notification_coros.append(_send_linear())

                if _notification_coros:
                    _notif_results = await asyncio.gather(*_notification_coros, return_exceptions=True)
                    _notif_names = ["email", "slack", "notion", "linear"]
                    for _name, _exc in zip(_notif_names, _notif_results):
                        if isinstance(_exc, Exception):
                            logger.warning("%s notification failed for bot %s: %s", _name, bot_id, _exc)

            # ── 7. done ───────────────────────────────────────────────────
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
            # Only delete audio if NOT stored as a recording (recording_path set means we keep it)
            try:
                if os.path.exists(audio_path) and not bot.recording_path:
                    os.remove(audio_path)
            except Exception:
                pass


async def _persist_action_items(db: AsyncSession, bot: Bot) -> None:
    """Upsert action items from bot.analysis into the ActionItem table."""
    try:
        items = (bot.analysis or {}).get("action_items", [])
        for item in items:
            task = (item.get("task") or "").strip()
            if not task:
                continue
            ai = ActionItem(
                bot_id=bot.id,
                task=task,
                assignee=(item.get("assignee") or None),
                due_date=(item.get("due_date") or None),
            )
            db.add(ai)
        if items:
            await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.warning("Failed to persist action items for bot %s: %s", bot.id, exc)
