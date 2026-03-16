"""Bot lifecycle management — drives a real browser bot through a meeting."""

import asyncio
import logging
import os
import re as _re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from app.config import settings
from app.store import store, BotSession, _now
from app.api.ws import manager as ws_manager
from app.services import intelligence_service, webhook_service
from app.services.browser_bot import run_browser_bot
from app.services.transcription_service import transcribe_audio
from app.services.intelligence_service import collect_usage

logger = logging.getLogger(__name__)

# Persistent recordings directory
_RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/app/data/recordings"))
try:
    _RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    _RECORDINGS_DIR = Path(tempfile.gettempdir()) / "meetingbot_recordings"
    _RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

_FILLER_WORDS = _re.compile(
    r'\b(um|uh|like|you know|so|basically|literally|actually|right)\b',
    _re.IGNORECASE,
)

_PLATFORM_NETLOC: dict[str, set[str]] = {
    "zoom":            {"zoom.us", "zoom.com"},
    "google_meet":     {"meet.google.com"},
    "microsoft_teams": {"teams.microsoft.com", "teams.live.com"},
    "webex":           {"webex.com", "cisco.webex.com"},
    "whereby":         {"whereby.com"},
    "bluejeans":       {"bluejeans.com"},
    "gotomeeting":     {"gotomeeting.com"},
}

_REAL_PLATFORMS = {"google_meet", "zoom", "microsoft_teams"}


def detect_platform(url: str) -> str:
    """Return platform key by matching the parsed netloc."""
    try:
        url = _unwrap_safelinks(url)
        netloc = urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return "unknown"
    for platform, hosts in _PLATFORM_NETLOC.items():
        if any(netloc == h or netloc.endswith("." + h) for h in hosts):
            return platform
    return "unknown"


def _unwrap_safelinks(url: str) -> str:
    try:
        parsed = urlparse(url)
        if "safelinks.protection.outlook.com" in parsed.netloc:
            qs = parse_qs(parsed.query)
            if "url" in qs:
                return unquote(qs["url"][0])
    except Exception:
        pass
    return url


def _compute_speaker_stats(transcript: list[dict]) -> list[dict]:
    if not transcript:
        return []
    entries = sorted(transcript, key=lambda e: e.get("timestamp", 0))

    speaker_time: dict[str, float] = {}
    speaker_questions: dict[str, int] = {}
    speaker_fillers: dict[str, int] = {}
    speaker_monologue: dict[str, float] = {}
    speaker_turns: dict[str, int] = {}

    for i, e in enumerate(entries):
        speaker = e.get("speaker", "Unknown")
        text = e.get("text", "")
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


def _flush_ai_usage(bot: BotSession) -> None:
    records = collect_usage()
    if records:
        bot.ai_usage.extend(records)


async def _set_status(bot: BotSession, status: str, **kwargs) -> None:
    """Update bot status in-memory and fire a webhook + WebSocket event."""
    kwargs["status"] = status
    await store.update_bot(bot.id, **kwargs)
    await webhook_service.dispatch_event(
        f"bot.{status}",
        {
            "bot_id":           bot.id,
            "bot_name":         bot.bot_name,
            "status":           status,
            "meeting_url":      bot.meeting_url,
            "meeting_platform": bot.meeting_platform,
            "ts":               _now().isoformat(),
        },
    )


def _resolve_prompt(bot: BotSession) -> str | None:
    """Return the analysis prompt to use, or None for the default."""
    if bot.prompt_override:
        return bot.prompt_override
    if bot.template:
        return intelligence_service.get_template_prompt(bot.template)
    return None


async def _do_analysis(bot: BotSession, audio_path: str, use_real_bot: bool) -> None:
    """Transcribe (if needed), analyse, and update the bot in-memory."""
    # ── transcript ─────────────────────────────────────────────────────────
    if not bot.transcript:
        transcript: list = []

        if use_real_bot and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            logger.info("Bot %s: transcribing partial audio (%d bytes)", bot.id, os.path.getsize(audio_path))
            transcript = await transcribe_audio(audio_path)

        if not transcript:
            logger.warning("Bot %s: no usable audio captured — transcript will be empty", bot.id)
            current = bot.error_message or ""
            await store.update_bot(
                bot.id,
                transcript=[],
                error_message=current + " No audio was captured or transcription returned no content.",
            )
        else:
            await store.update_bot(bot.id, transcript=transcript)
            bot.transcript = transcript

        await webhook_service.dispatch_event(
            "bot.transcript_ready",
            {"bot_id": bot.id, "entry_count": len(transcript)},
        )

    # ── analysis ───────────────────────────────────────────────────────────
    analysis_mode = bot.analysis_mode or "full"
    if not bot.analysis and bot.transcript and analysis_mode != "transcript_only":
        prompt_override = _resolve_prompt(bot)
        analysis_result, chapters_result = await asyncio.gather(
            intelligence_service.analyze_transcript(
                bot.transcript,
                prompt_override=prompt_override,
                vocabulary=bot.vocabulary or [],
            ),
            intelligence_service.generate_chapters(bot.transcript),
            return_exceptions=True,
        )

        if isinstance(analysis_result, Exception):
            logger.error("Analysis failed for bot %s: %s", bot.id, analysis_result)
            analysis_result = {
                "summary": "Analysis unavailable — an error occurred during processing.",
                "key_points": [], "action_items": [], "decisions": [],
                "next_steps": [], "sentiment": "neutral", "topics": [],
            }
            current = bot.error_message or ""
            await store.update_bot(
                bot.id,
                error_message=current + f" [analysis error: {analysis_result}]",
            )

        chapters = [] if isinstance(chapters_result, Exception) else (chapters_result or [])
        speaker_stats = _compute_speaker_stats(bot.transcript)

        if use_real_bot and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            await store.update_bot(bot.id, recording_path=audio_path)
            bot.recording_path = audio_path

        await store.update_bot(
            bot.id,
            analysis=analysis_result,
            chapters=chapters,
            speaker_stats=speaker_stats,
        )
        bot.analysis = analysis_result

        await webhook_service.dispatch_event("bot.analysis_ready", {"bot_id": bot.id})

    elif analysis_mode == "transcript_only":
        logger.info("Bot %s: analysis_mode=transcript_only — skipping AI analysis", bot.id)
        speaker_stats = _compute_speaker_stats(bot.transcript)
        if use_real_bot and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            await store.update_bot(bot.id, recording_path=audio_path, speaker_stats=speaker_stats)
            bot.recording_path = audio_path
        else:
            await store.update_bot(bot.id, speaker_stats=speaker_stats)


def _build_done_payload(bot: BotSession) -> dict:
    """Build the full webhook payload delivered to the per-bot webhook_url on completion."""
    return {
        "bot_id":           bot.id,
        "meeting_url":      bot.meeting_url,
        "meeting_platform": bot.meeting_platform,
        "bot_name":         bot.bot_name,
        "status":           bot.status,
        "participants":     bot.participants,
        "transcript":       bot.transcript,
        "analysis":         bot.analysis,
        "chapters":         bot.chapters,
        "speaker_stats":    bot.speaker_stats,
        "duration_seconds": bot.duration_seconds,
        "recording_available": bot.recording_available(),
        "is_demo_transcript": bot.is_demo_transcript,
        "metadata":         bot.metadata,
        "ai_usage": {
            "total_tokens":   bot.ai_total_tokens,
            "total_cost_usd": bot.ai_total_cost_usd,
            "primary_model":  bot.ai_primary_model,
            "operations":     bot.ai_usage,
        },
        "ts": _now().isoformat(),
    }


async def run_bot_lifecycle(bot_id: str) -> None:
    """
    Full bot lifecycle:
      (scheduled →) joining → in_call → call_ended → done

    Real platforms (Google Meet, Zoom, Teams):
      Playwright browser bot joins, records audio, Gemini transcribes.

    Unsupported platforms:
      Simulated lifecycle with a Gemini-generated demo transcript.
    """
    bot = await store.get_bot(bot_id)
    if bot is None:
        logger.error("Bot %s not found in store", bot_id)
        return

    audio_path = str(_RECORDINGS_DIR / f"{bot_id}.wav")
    use_real_bot = bot.meeting_platform in _REAL_PLATFORMS

    try:
        # ── 0. Scheduled — wait until join_at ─────────────────────────────
        if bot.join_at:
            delay = (bot.join_at.replace(tzinfo=timezone.utc) - _now()).total_seconds()
            if delay > 86400:
                logger.warning("Bot %s join_at > 24 h away — joining immediately", bot_id)
            elif delay > 0:
                logger.info("Bot %s scheduled — waiting %.0f s", bot_id, delay)
                await asyncio.sleep(delay)

        # ── 1. Joining ────────────────────────────────────────────────────
        await _set_status(bot, "joining")
        logger.info("Bot %s joining %s (%s)", bot_id, bot.meeting_url, bot.meeting_platform)

        if use_real_bot:
            admitted = False

            async def on_admitted() -> None:
                nonlocal admitted
                admitted = True
                await _set_status(bot, "in_call", started_at=_now())
                logger.info("Bot %s is now in_call", bot_id)

            _live_buffer: list = []
            _last_flush: float = time.monotonic()
            _live_lock = asyncio.Lock()

            async def on_live_entry(entry: dict) -> None:
                nonlocal _last_flush
                async with _live_lock:
                    _live_buffer.append(entry)
                    should_flush = (
                        len(_live_buffer) >= 10
                        or time.monotonic() - _last_flush >= 30
                    )
                await ws_manager.broadcast("bot.live_transcript", {"bot_id": bot_id, "entry": entry})
                if should_flush:
                    async with _live_lock:
                        snapshot = list(_live_buffer)
                    await store.update_bot(bot_id, transcript=snapshot)
                    _last_flush = time.monotonic()

            max_retries = settings.BOT_JOIN_MAX_RETRIES
            retry_delay = settings.BOT_JOIN_RETRY_DELAY_S
            last_error: str = ""

            for attempt in range(max_retries + 1):
                if attempt > 0:
                    logger.info("Bot %s join attempt %d/%d…", bot_id, attempt + 1, max_retries + 1)
                    await asyncio.sleep(retry_delay)
                    admitted = False
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
                    respond_on_mention=bot.respond_on_mention,
                    mention_response_mode=bot.mention_response_mode,
                    tts_provider=bot.tts_provider,
                    start_muted=bot.start_muted,
                    live_transcription=bot.live_transcription,
                    on_live_transcript_entry=on_live_entry,
                    gemini_api_key=settings.GEMINI_API_KEY or "",
                )

                if bot_result["success"]:
                    break

                last_error = bot_result["error"] or "Browser bot failed"
                if admitted:
                    break
                if attempt < max_retries:
                    logger.warning("Bot %s join failed (attempt %d): %s", bot_id, attempt + 1, last_error)
                else:
                    raise RuntimeError(last_error)

            if not bot_result["success"] and not admitted:
                raise RuntimeError(last_error or "Browser bot failed after all retries")

            # ── 2. call_ended → transcribe ────────────────────────────────
            scraped_participants: list[str] = bot_result.get("participants") or []
            live_transcript_entries: list = bot_result.get("live_transcript") or []

            async with _live_lock:
                final_buffer = list(_live_buffer)
            if final_buffer:
                await store.update_bot(bot_id, transcript=final_buffer)
                logger.info("Bot %s: flushed %d live transcript entries", bot_id, len(_live_buffer))

            await _set_status(bot, "call_ended", ended_at=_now())
            await store.update_bot(bot_id, status="transcribing")
            logger.info("Bot %s transcribing audio…", bot_id)

            transcript = await transcribe_audio(
                audio_path,
                known_participants=scraped_participants,
                language=settings.TRANSCRIPTION_LANGUAGE or None,
            )

            if live_transcript_entries:
                if len(transcript) < 3:
                    logger.info("Bot %s: batch sparse — using %d live entries", bot_id, len(live_transcript_entries))
                    transcript = live_transcript_entries
                else:
                    batch_texts = {e.get("text", "").strip().lower() for e in transcript}
                    extra = [e for e in live_transcript_entries if e.get("text", "").strip().lower() not in batch_texts]
                    if extra:
                        transcript = sorted(transcript + extra, key=lambda e: e.get("timestamp", 0))

            if not transcript:
                logger.warning("Bot %s: Gemini returned empty transcript", bot_id)
                current = bot.error_message or ""
                await store.update_bot(
                    bot_id,
                    error_message=current + " Transcription returned no content.",
                )

        else:
            # ── Unsupported platform — demo mode ──────────────────────────
            logger.info("Platform '%s' not supported — demo mode", bot.meeting_platform)
            await asyncio.sleep(3)
            await _set_status(bot, "in_call", started_at=_now())
            await asyncio.sleep(settings.BOT_SIMULATION_DURATION)
            await _set_status(bot, "call_ended", ended_at=_now())
            await store.update_bot(bot_id, status="transcribing")
            transcript = await intelligence_service.generate_demo_transcript(bot.meeting_url)
            scraped_participants = []
            await store.update_bot(bot_id, is_demo_transcript=True)
            bot.is_demo_transcript = True

        # Flush AI usage from transcription
        _flush_ai_usage(bot)

        # ── 3. Store transcript + participants ────────────────────────────
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

        await store.update_bot(bot_id, transcript=transcript, participants=sorted(unique_names))
        bot.transcript = transcript
        bot.participants = sorted(unique_names)

        await webhook_service.dispatch_event(
            "bot.transcript_ready",
            {"bot_id": bot_id, "entry_count": len(transcript)},
        )

        # ── 4. Analysis + chapters + speaker stats ────────────────────────
        bot_refreshed = await store.get_bot(bot_id)
        if bot_refreshed:
            bot = bot_refreshed

        await _do_analysis(bot, audio_path, use_real_bot)

        # Flush AI usage from analysis
        _flush_ai_usage(bot)

        # Refresh bot state from store to get latest fields
        bot_refreshed = await store.get_bot(bot_id)
        if bot_refreshed:
            bot = bot_refreshed

        # ── 5. Compute meeting duration ───────────────────────────────────
        # (duration_seconds is a computed property on BotSession)

        # ── 6. Done ───────────────────────────────────────────────────────
        await store.mark_terminal(bot_id, "done", ended_at=bot.ended_at or _now())
        bot = await store.get_bot(bot_id)

        # Deduct credits for the completed bot run
        from app.services.credit_service import deduct_credits_for_bot
        await deduct_credits_for_bot(bot.account_id, bot_id, bot.ai_total_cost_usd)

        # Dispatch done event — both global webhooks and per-bot webhook_url
        done_payload = _build_done_payload(bot)
        await webhook_service.dispatch_event(
            "bot.done",
            done_payload,
            extra_webhook_url=bot.webhook_url,
        )
        logger.info("Bot %s done", bot_id)

    except asyncio.CancelledError:
        logger.info("Bot %s cancelled — salvaging transcript", bot_id)
        bot = await store.get_bot(bot_id)
        if bot:
            await store.update_bot(bot_id, status="transcribing")
            try:
                await _do_analysis(bot, audio_path, use_real_bot)
                _flush_ai_usage(bot)
                bot = await store.get_bot(bot_id)
            except Exception:
                logger.exception("Bot %s: error during cancellation cleanup", bot_id)
            try:
                await store.mark_terminal(bot_id, "cancelled", ended_at=bot.ended_at or _now())
                bot = await store.get_bot(bot_id)
                from app.services.credit_service import deduct_credits_for_bot
                await deduct_credits_for_bot(bot.account_id, bot_id, bot.ai_total_cost_usd)
                done_payload = _build_done_payload(bot)
                await webhook_service.dispatch_event(
                    "bot.cancelled",
                    done_payload,
                    extra_webhook_url=bot.webhook_url,
                )
            except Exception:
                pass

    except Exception as exc:
        logger.exception("Bot %s error: %s", bot_id, exc)
        bot = await store.get_bot(bot_id)
        if bot:
            await store.update_bot(bot_id, status="transcribing")
            try:
                await _do_analysis(bot, audio_path, use_real_bot)
                _flush_ai_usage(bot)
                bot = await store.get_bot(bot_id)
            except Exception:
                logger.exception("Bot %s: error during error cleanup", bot_id)
            try:
                await store.mark_terminal(
                    bot_id, "error",
                    error_message=str(exc),
                    ended_at=bot.ended_at if bot else _now(),
                )
                bot = await store.get_bot(bot_id)
                if bot:
                    from app.services.credit_service import deduct_credits_for_bot
                    await deduct_credits_for_bot(bot.account_id, bot_id, bot.ai_total_cost_usd)
                    done_payload = _build_done_payload(bot)
                    await webhook_service.dispatch_event(
                        "bot.error",
                        done_payload,
                        extra_webhook_url=bot.webhook_url,
                    )
            except Exception:
                pass

    finally:
        # Delete audio only if it was NOT stored as a persistent recording
        try:
            current_bot = await store.get_bot(bot_id)
            if os.path.exists(audio_path) and not (current_bot and current_bot.recording_path):
                os.remove(audio_path)
        except Exception:
            pass
