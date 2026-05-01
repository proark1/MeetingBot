"""In-meeting @bot chat Q&A handler.

When ``BotSession.enable_chat_qa`` is true, every incoming chat message is
inspected for the configured trigger prefix (default ``@bot``). If a
trigger is detected the bot answers using the live transcript as context
and posts the reply back to the meeting chat (and optionally speaks it
via TTS).

Throttled per-bot using ``BotSession.chat_qa_last_reply_ts`` so a
participant can't flood the bot.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from app.services import intelligence_service

logger = logging.getLogger(__name__)


def matches_trigger(text: str, trigger: str) -> Optional[str]:
    """Return the question stripped of the trigger prefix, or None when no
    trigger is present."""
    if not text or not trigger:
        return None
    lowered = text.strip()
    if not lowered.lower().startswith(trigger.lower()):
        return None
    rest = lowered[len(trigger):].strip()
    # Allow common separators after the trigger word: "@bot, what...", "@bot:"
    while rest and rest[0] in ",.:;-":
        rest = rest[1:].strip()
    return rest or None


async def handle_chat_entry(bot, entry: dict) -> Optional[dict]:
    """Process a chat entry. Returns the reply record on success, else None.

    Keeps state on the BotSession (``chat_qa_last_reply_ts``) for throttling.
    """
    if not getattr(bot, "enable_chat_qa", False):
        return None
    if entry.get("source") != "chat":
        return None

    cfg = getattr(bot, "chat_qa_config", None) or {}
    trigger = cfg.get("trigger", "@bot")
    rate_limit = int(cfg.get("rate_limit_seconds", 10))
    reply_via = cfg.get("reply_via", "chat")

    text = entry.get("text") or ""
    question = matches_trigger(text, trigger)
    if not question:
        return None

    now_mono = time.monotonic()
    last = float(getattr(bot, "chat_qa_last_reply_ts", 0.0) or 0.0)
    if rate_limit > 0 and now_mono - last < rate_limit:
        logger.debug(
            "Chat-QA throttled for bot %s (%.1fs since last)",
            bot.id, now_mono - last,
        )
        return None

    transcript = list(getattr(bot, "transcript", []) or [])
    answer = await intelligence_service.ask_about_transcript(transcript, question)
    if not answer:
        answer = "I couldn't generate an answer for that question."

    # Mark last reply *before* the post so concurrent triggers still throttle.
    bot.chat_qa_last_reply_ts = now_mono

    return {
        "question": question,
        "answer": answer,
        "asker": entry.get("speaker"),
        "reply_via": reply_via,
        "ts": entry.get("timestamp"),
    }


async def deliver_reply(bot, reply: dict) -> bool:
    """Deliver a chat-QA reply via the bot's runtime handle.

    Returns True when at least one delivery channel succeeded.
    """
    runtime = getattr(bot, "runtime", None)
    if not runtime:
        logger.debug("Chat-QA reply for bot %s: no runtime handle (bot not in_call)", bot.id)
        return False

    answer = reply.get("answer") or ""
    if not answer:
        return False

    delivered = False
    reply_via = reply.get("reply_via", "chat")
    page = runtime.get("page")
    chat_lock = runtime.get("chat_lock")

    if reply_via in ("chat", "both") and page is not None and chat_lock is not None:
        try:
            from app.services.browser_bot import _send_chat_message
            async with chat_lock:
                ok = await _send_chat_message(page, runtime.get("platform"), answer)
            if ok:
                delivered = True
        except Exception as exc:
            logger.warning("Chat-QA chat delivery failed for bot %s: %s", bot.id, exc)

    if reply_via in ("voice", "both"):
        try:
            from app.services.browser_bot import _speak_in_meeting
            speak_lock = runtime.get("speak_lock")
            if speak_lock is not None and page is not None:
                async with speak_lock:
                    await _speak_in_meeting(
                        page, runtime.get("platform"),
                        answer,
                        tts_provider=runtime.get("tts_provider", "edge"),
                        gemini_api_key=runtime.get("gemini_api_key"),
                        pulse_mic=runtime.get("pulse_mic"),
                        start_muted=runtime.get("start_muted", False),
                    )
                delivered = True
        except Exception as exc:
            logger.warning("Chat-QA voice delivery failed for bot %s: %s", bot.id, exc)

    return delivered
