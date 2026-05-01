"""Agentic delegation engine.

Lets a user send a bot to a meeting on their behalf with a list of
natural-language instructions. During the meeting the bot evaluates each
instruction against the live transcript and decides whether to act
(speak via TTS or post in chat).

Instruction triggers:
  - ``manual``      — only when the API endpoint /agentic/trigger fires it
  - ``on_topic``    — when an LLM judges the instruction's topic is being
                      discussed in the recent transcript window
  - ``on_silence``  — after ``interval_seconds`` of silence
  - ``on_interval`` — periodically every ``interval_seconds``

Autonomy gating mirrors ``BotSession.agentic_autonomy``:
  - ``off``    → ignore everything
  - ``low``    → only manual triggers
  - ``medium`` → low + on_topic
  - ``high``   → medium + on_silence + on_interval
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from app.services import intelligence_service

logger = logging.getLogger(__name__)


_AUTONOMY_TRIGGERS: dict[str, set[str]] = {
    "off":    set(),
    "low":    {"manual"},
    "medium": {"manual", "on_topic"},
    "high":   {"manual", "on_topic", "on_silence", "on_interval"},
}


class AgenticEngine:
    """One per bot. Stateless across processes — relies on BotSession persistence."""

    def __init__(self, bot) -> None:
        self.bot = bot
        # Per-instruction next-eval timestamps (monotonic seconds).
        self._next_eval: dict[int, float] = {}
        self._last_entry_mono: float = time.monotonic()
        self._invocations: dict[int, int] = dict(getattr(bot, "agentic_invocations", {}) or {})

    def _allowed(self, trigger: str) -> bool:
        autonomy = getattr(self.bot, "agentic_autonomy", "off") or "off"
        return trigger in _AUTONOMY_TRIGGERS.get(autonomy, set())

    def _enabled(self) -> bool:
        return bool(getattr(self.bot, "agentic_instructions", []))

    async def feed(self, entry: Optional[dict] = None) -> list[dict]:
        """Process either a new transcript entry or a tick (entry=None).

        Returns a list of action records ready for delivery::

            {"index": int, "instruction": str, "action": "speak"|"chat", "text": str}
        """
        if not self._enabled():
            return []

        autonomy = getattr(self.bot, "agentic_autonomy", "off") or "off"
        if autonomy == "off":
            return []

        if entry is not None:
            self._last_entry_mono = time.monotonic()

        actions: list[dict] = []
        instructions = list(getattr(self.bot, "agentic_instructions", []) or [])
        now_mono = time.monotonic()

        for idx, instr in enumerate(instructions):
            if not isinstance(instr, dict):
                continue
            trig = instr.get("trigger", "on_topic")
            if not self._allowed(trig):
                continue
            max_inv = int(instr.get("max_invocations") or 0)
            if max_inv and self._invocations.get(idx, 0) >= max_inv:
                continue

            interval = int(instr.get("interval_seconds") or 0)
            next_eval = self._next_eval.get(idx, 0.0)

            should_eval = False
            if trig == "on_interval" and interval > 0 and now_mono >= next_eval:
                should_eval = True
            elif trig == "on_silence" and interval > 0:
                if now_mono - self._last_entry_mono >= interval and now_mono >= next_eval:
                    should_eval = True
            elif trig == "on_topic" and entry is not None and now_mono >= next_eval:
                should_eval = True
            elif trig == "manual":
                # Only the explicit /agentic/trigger endpoint fires manual.
                continue

            if not should_eval:
                continue

            response = await self._evaluate(instr, entry)
            if not response:
                continue
            self._invocations[idx] = self._invocations.get(idx, 0) + 1
            # Floor matches AgenticInstruction.interval_seconds (ge=15) in
            # backend/app/schemas/bot.py — using 30 here would silently
            # suppress user-configured intervals between 15 and 30 s.
            self._next_eval[idx] = now_mono + max(15, interval or 60)
            actions.append({
                "index": idx,
                "instruction": instr.get("instruction", ""),
                "action": "speak" if instr.get("speak") else "chat",
                "text": response,
                "trigger": trig,
            })

        # Persist the invocation counter back onto the bot.
        if actions:
            try:
                self.bot.agentic_invocations = dict(self._invocations)
            except Exception:
                pass

        return actions

    async def trigger_manual(self, index: int) -> Optional[dict]:
        """Force-evaluate a specific instruction. Bypasses autonomy gating
        for ``manual`` triggers but still respects ``max_invocations``."""
        instructions = list(getattr(self.bot, "agentic_instructions", []) or [])
        if not 0 <= index < len(instructions):
            return None
        instr = instructions[index]
        max_inv = int(instr.get("max_invocations") or 0)
        if max_inv and self._invocations.get(index, 0) >= max_inv:
            return None
        response = await self._evaluate(instr, None)
        if not response:
            return None
        self._invocations[index] = self._invocations.get(index, 0) + 1
        try:
            self.bot.agentic_invocations = dict(self._invocations)
        except Exception:
            pass
        return {
            "index": index,
            "instruction": instr.get("instruction", ""),
            "action": "speak" if instr.get("speak") else "chat",
            "text": response,
            "trigger": "manual",
        }

    async def _evaluate(self, instr: dict, entry: Optional[dict]) -> Optional[str]:
        """Ask the LLM whether to act on this instruction and what to say.

        For ``on_topic`` triggers, the recent transcript is included so the
        LLM can decide if the topic is currently in scope.
        """
        try:
            transcript = list(getattr(self.bot, "transcript", []) or [])[-20:]
            recent = "\n".join(
                f"{e.get('speaker', '?')}: {e.get('text', '')}" for e in transcript
            ) or "(no transcript yet)"

            prompt = (
                f"You are a meeting attendee acting on the user's behalf. The user "
                f"asked you to: {instr.get('instruction', '')}\n\n"
                f"Recent meeting transcript:\n{recent}\n\n"
                "Decide:\n"
                "1. Is now an appropriate moment to act on the instruction? "
                "(only act when truly relevant — never spam)\n"
                "2. If yes, write a short, polite response (max 2 sentences) "
                "that you would say or post in chat.\n\n"
                "Return ONLY JSON: "
                '{"act": true|false, "response": "..."}.'
            )

            if intelligence_service._use_claude():
                text = await intelligence_service._claude_fast_complete(
                    prompt, max_tokens=256, operation="agentic_eval"
                )
            elif intelligence_service._use_gemini():
                model = intelligence_service._get_gemini_model()
                response = await model.generate_content_async(
                    prompt,
                    generation_config={"temperature": 0.3, "max_output_tokens": 256},
                )
                text = response.text
            else:
                return None

            import json as _json
            parsed = _json.loads(intelligence_service._strip_fences(text or ""))
            if not parsed.get("act"):
                return None
            answer = (parsed.get("response") or "").strip()
            return answer or None
        except Exception as exc:
            logger.debug("Agentic evaluation failed: %s", exc)
            return None


async def deliver_action(bot, action: dict) -> bool:
    """Speak or post the action through the bot's runtime handle."""
    runtime = getattr(bot, "runtime", None)
    if not runtime:
        return False
    text = action.get("text") or ""
    if not text:
        return False
    page = runtime.get("page")

    if action.get("action") == "speak":
        try:
            from app.services.browser_bot import _speak_in_meeting
            speak_lock = runtime.get("speak_lock")
            if speak_lock is None or page is None:
                return False
            async with speak_lock:
                await _speak_in_meeting(
                    page, runtime.get("platform"), text,
                    tts_provider=runtime.get("tts_provider", "edge"),
                    gemini_api_key=runtime.get("gemini_api_key"),
                    pulse_mic=runtime.get("pulse_mic"),
                    start_muted=runtime.get("start_muted", False),
                )
            return True
        except Exception as exc:
            logger.warning("Agentic speak delivery failed for bot %s: %s", bot.id, exc)
            return False

    # default: chat
    try:
        from app.services.browser_bot import _send_chat_message
        chat_lock = runtime.get("chat_lock")
        if chat_lock is None or page is None:
            return False
        async with chat_lock:
            ok = await _send_chat_message(page, runtime.get("platform"), text)
        return bool(ok)
    except Exception as exc:
        logger.warning("Agentic chat delivery failed for bot %s: %s", bot.id, exc)
        return False
