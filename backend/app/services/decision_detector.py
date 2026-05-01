"""Smart decision/action moment detection from a live transcript stream.

Cheap heuristic-first detection (regex over the entry text) followed by an
optional AI confirmation pass. Designed to be opted into per-bot via
``BotSession.enable_decision_detection``.

Emits structured records of the form::

    {
        "kind": "decision" | "action",
        "text": "<the matched sentence>",
        "speaker": "<speaker name>",
        "timestamp": <float seconds from meeting start>,
        "confidence": <float in [0, 1]>,
    }

Records are appended to ``BotSession.detected_decisions`` and broadcast as a
``bot.decision_detected`` webhook + WebSocket event.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Patterns are ordered by specificity. Each entry is (kind, regex, confidence).
# Phrase-anchored to keep noise low — generic verbs like "do" / "make" are
# avoided unless paired with a commitment word.
_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("decision",  re.compile(r"\b(we (?:have|'ve)? ?(?:decided|agreed) (?:to|that))\b", re.I), 0.9),
    ("decision",  re.compile(r"\b(let'?s (?:go with|move forward with|ship|approve))\b", re.I), 0.85),
    ("decision",  re.compile(r"\bdecision[:\-]\s+", re.I), 0.95),
    ("decision",  re.compile(r"\b(final(?:ly|ised|ized)?\s+(?:we|I) (?:will|'ll|are going to))\b", re.I), 0.7),
    ("action",    re.compile(r"\baction[:\-]\s+", re.I), 0.95),
    ("action",    re.compile(r"\b(I will|I'll|I'm going to|we will|we'll)\s+(?!be (?:able|going) to\b)", re.I), 0.7),
    ("action",    re.compile(r"\b(?:can|could|would) you\s+(?:please\s+)?(?:send|share|review|update|prepare|schedule|book|confirm|forward|follow up)\b", re.I), 0.75),
    ("action",    re.compile(r"\b(?:owner|assignee|to do)[:\-]\s+", re.I), 0.85),
    ("action",    re.compile(r"\bnext step[s]?[:\-]\s+", re.I), 0.8),
]

# Filter out trivial matches (very short, just a stop-word, etc.).
_MIN_TEXT_LEN = 12


def detect(entry: dict) -> list[dict]:
    """Run heuristic decision/action detection on a single transcript entry.

    Returns 0+ records. Empty list when the entry text is too short or no
    pattern matches.
    """
    text = (entry.get("text") or "").strip()
    if len(text) < _MIN_TEXT_LEN:
        return []

    speaker = entry.get("speaker") or "Unknown"
    timestamp = float(entry.get("timestamp") or 0.0)
    matches: list[dict] = []
    seen_kinds: set[str] = set()

    for kind, pattern, confidence in _PATTERNS:
        if pattern.search(text):
            # One detection per (kind) per entry — avoids duplicate webhook spam.
            if kind in seen_kinds:
                continue
            seen_kinds.add(kind)
            matches.append({
                "kind": kind,
                "text": text,
                "speaker": speaker,
                "timestamp": timestamp,
                "confidence": confidence,
                "source": entry.get("source", "voice"),
            })

    return matches


async def confirm_with_ai(record: dict) -> Optional[dict]:
    """Optional AI confirmation pass — returns the record (possibly with
    adjusted confidence) or None if the AI rejects it.

    Cheap call: Gemini Flash / Claude Haiku. Falls back to passing the
    heuristic record through when no provider is available.
    """
    try:
        from app.services import intelligence_service

        prompt = (
            "Classify the following meeting utterance. Return ONLY JSON: "
            '{"kind": "decision"|"action"|"none", "confidence": 0.0-1.0}.\n\n'
            f"Utterance ({record['speaker']}): {record['text']}"
        )

        # Use the cheap fast complete path
        if intelligence_service._use_claude():
            text = await intelligence_service._claude_fast_complete(prompt, max_tokens=64, operation="decision_confirm")
        elif intelligence_service._use_gemini():
            model = intelligence_service._get_gemini_model()
            response = await model.generate_content_async(
                prompt,
                generation_config={"temperature": 0.0, "max_output_tokens": 64},
            )
            text = response.text
        else:
            return record

        import json as _json
        cleaned = intelligence_service._strip_fences(text or "")
        parsed = _json.loads(cleaned)
        kind = parsed.get("kind", "none").lower()
        if kind == "none":
            return None
        record["kind"] = kind if kind in ("decision", "action") else record["kind"]
        record["confidence"] = float(parsed.get("confidence", record["confidence"]))
        return record
    except Exception as exc:
        logger.debug("Decision AI confirmation failed: %s", exc)
        return record
