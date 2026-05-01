"""Live per-speaker analytics aggregator.

Keeps a rolling, in-memory tally of:
  - total spoken seconds per speaker
  - interruption count per speaker
  - filler-word count per speaker
  - (optional) average sentiment per speaker

A single ``SpeakerAnalyticsAggregator`` instance is created per bot when
``BotSession.enable_speaker_analytics`` is true. It is fed entries from
``bot_service.on_live_entry`` and emits a snapshot every
``interval_seconds`` (configurable per bot) via the SSE manager + an
optional ``bot.speaker_analytics`` webhook event.

Sentiment, when enabled, is computed asynchronously to avoid blocking the
live-entry hot path.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

_FILLER_WORDS = re.compile(
    r"\b(um+|uh+|like|you know|so|basically|literally|actually|right)\b",
    re.IGNORECASE,
)


class SpeakerAnalyticsAggregator:
    """Per-bot live aggregator. Not thread-safe — call from a single asyncio task."""

    def __init__(
        self,
        bot_id: str,
        account_id: Optional[str],
        interval_seconds: int = 30,
        include_sentiment: bool = False,
        include_interruptions: bool = True,
    ) -> None:
        self.bot_id = bot_id
        self.account_id = account_id
        self.interval_seconds = max(5, int(interval_seconds))
        self.include_sentiment = bool(include_sentiment)
        self.include_interruptions = bool(include_interruptions)

        self._last_emit = time.monotonic()
        self._last_speaker: Optional[str] = None
        self._last_speaker_end_ts: float = 0.0
        self._prev_entry_ts: float = 0.0
        # Tally — per speaker
        self._talk_seconds: dict[str, float] = {}
        self._interruptions: dict[str, int] = {}
        self._filler_counts: dict[str, int] = {}
        self._sentiment_sum: dict[str, float] = {}
        self._sentiment_n: dict[str, int] = {}
        self._utterances: dict[str, int] = {}

    async def feed(self, entry: dict) -> Optional[dict]:
        """Process a single transcript entry. Returns a snapshot dict if it's
        time to emit, else ``None``."""
        if entry.get("source") == "chat":
            return None  # chat messages don't count toward talk time

        speaker = (entry.get("speaker") or "Unknown").strip() or "Unknown"
        text = entry.get("text") or ""
        ts = float(entry.get("timestamp") or 0.0)

        # Estimate utterance duration as the gap from previous entry, capped.
        gap = ts - self._prev_entry_ts if self._prev_entry_ts > 0 else 3.0
        duration = max(0.5, min(gap, 60.0))
        self._prev_entry_ts = ts

        self._talk_seconds[speaker] = self._talk_seconds.get(speaker, 0.0) + duration
        self._utterances[speaker] = self._utterances.get(speaker, 0) + 1

        # Interruption: speaker switched within 1.5s of the previous speaker.
        if self.include_interruptions and self._last_speaker and self._last_speaker != speaker:
            if ts - self._last_speaker_end_ts < 1.5:
                self._interruptions[speaker] = self._interruptions.get(speaker, 0) + 1

        self._last_speaker = speaker
        self._last_speaker_end_ts = ts

        # Filler word count
        fillers = len(_FILLER_WORDS.findall(text))
        if fillers:
            self._filler_counts[speaker] = self._filler_counts.get(speaker, 0) + fillers

        # Should we emit?
        now_mono = time.monotonic()
        if now_mono - self._last_emit < self.interval_seconds:
            return None
        self._last_emit = now_mono
        return self.snapshot()

    def snapshot(self) -> dict:
        total_talk = sum(self._talk_seconds.values()) or 1.0
        speakers = []
        for spkr, secs in sorted(self._talk_seconds.items(), key=lambda kv: kv[1], reverse=True):
            sentiment_avg = None
            if self._sentiment_n.get(spkr):
                sentiment_avg = round(self._sentiment_sum[spkr] / self._sentiment_n[spkr], 3)
            speakers.append({
                "speaker": spkr,
                "talk_seconds": round(secs, 1),
                "talk_pct": round((secs / total_talk) * 100, 1),
                "utterances": self._utterances.get(spkr, 0),
                "interruptions": self._interruptions.get(spkr, 0),
                "filler_words": self._filler_counts.get(spkr, 0),
                "sentiment_avg": sentiment_avg,
            })
        return {
            "bot_id": self.bot_id,
            "total_talk_seconds": round(total_talk, 1),
            "speakers": speakers,
            "captured_at_mono": round(time.monotonic(), 2),
        }

    async def record_sentiment(self, speaker: str, score: float) -> None:
        """Optional: feed a sentiment score for a recent utterance."""
        self._sentiment_sum[speaker] = self._sentiment_sum.get(speaker, 0.0) + float(score)
        self._sentiment_n[speaker] = self._sentiment_n.get(speaker, 0) + 1


async def quick_sentiment(text: str) -> Optional[float]:
    """Cheap sentiment scoring on a single utterance.

    Returns a float in [-1, 1] or None if no AI provider is available.
    Used opportunistically — failures are swallowed.
    """
    if not text or len(text) < 8:
        return None
    try:
        from app.services import intelligence_service
        prompt = (
            "Score the sentiment of the following utterance from -1 (very negative) "
            "to 1 (very positive). Return ONLY a number, no prose, no quotes.\n\n"
            f"Utterance: {text}"
        )
        if intelligence_service._use_gemini():
            model = intelligence_service._get_gemini_model()
            response = await model.generate_content_async(
                prompt,
                generation_config={"temperature": 0.0, "max_output_tokens": 8},
            )
            raw = (response.text or "").strip().split()[0]
        elif intelligence_service._use_claude():
            raw = await intelligence_service._claude_fast_complete(
                prompt, max_tokens=8, operation="speaker_sentiment"
            )
            raw = (raw or "").strip().split()[0]
        else:
            return None
        return max(-1.0, min(1.0, float(raw)))
    except Exception:
        return None
