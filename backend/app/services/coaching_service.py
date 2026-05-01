"""Private host-coaching engine.

Watches a live transcript stream and emits actionable tips to a single host
participant. Tips cover:

- ``talk_time``     — host is dominating (or barely talking)
- ``monologue``     — host has been speaking continuously > N seconds
- ``filler_words``  — high filler-word ratio in last window
- ``silence``       — long stretches of silence
- ``interruptions`` — host repeatedly cut off others
- ``sentiment``     — sentiment dropped sharply
- ``pace``          — speaking too fast / too slow

Tips are pushed to a dedicated SSE channel (``coaching:{bot_id}``) so only
the host UI receives them. They can also be fanned out via webhook when the
bot's ``coaching_config.deliver_via`` is ``webhook`` or ``both``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# Sliding window length (seconds) over which talk_time dominance is computed.
# Larger = more stable; smaller = more reactive to recent shifts.
_TALK_WINDOW_SECONDS = 300.0

_FILLER_RE = re.compile(
    r"\b(um+|uh+|like|you know|so|basically|literally|actually|right)\b",
    re.IGNORECASE,
)

_DEFAULT_METRICS = {"talk_time", "filler_words", "monologue"}
_VALID_METRICS = {"talk_time", "interruptions", "filler_words", "silence", "sentiment", "monologue", "pace"}


class CoachingEngine:
    """Per-bot coaching engine. Designed to be fed entries one at a time."""

    def __init__(
        self,
        bot_id: str,
        account_id: Optional[str],
        host_speaker_name: Optional[str] = None,
        metrics: Optional[list[str]] = None,
        nudge_interval_seconds: int = 120,
    ) -> None:
        self.bot_id = bot_id
        self.account_id = account_id
        self.host_speaker_name = host_speaker_name  # may be auto-detected on first entry
        self.metrics = set(metrics or _DEFAULT_METRICS) & _VALID_METRICS or _DEFAULT_METRICS
        self.nudge_interval = max(30, int(nudge_interval_seconds))

        self._last_fired: dict[str, float] = {}  # metric -> monotonic ts
        # Sliding window: each entry is (mono_ts_added, speaker, duration_s).
        # Older entries are evicted in _trim_window so the talk-time tally
        # always reflects only the last _TALK_WINDOW_SECONDS, regardless of
        # whether activity straddles a tumbling boundary.
        self._talk_events: deque[tuple[float, str, float]] = deque()
        self._monologue_run_start: Optional[float] = None  # ts when host started current monologue
        self._last_speaker: Optional[str] = None
        self._last_entry_mono: float = time.monotonic()
        self._filler_total: int = 0
        self._words_total: int = 0
        self._prev_entry_ts: float = 0.0

    def set_host(self, name: str) -> None:
        self.host_speaker_name = name

    async def feed(self, entry: dict, participants: Optional[list[str]] = None) -> list[dict]:
        """Process one entry. Returns 0+ coaching tips ready for delivery."""
        speaker = (entry.get("speaker") or "Unknown").strip() or "Unknown"
        text = entry.get("text") or ""
        ts = float(entry.get("timestamp") or 0.0)

        # Auto-detect host = first non-bot participant if not configured.
        if not self.host_speaker_name and participants:
            non_bot = [p for p in participants if p and "JustHereToListen" not in p]
            if non_bot:
                self.host_speaker_name = non_bot[0]

        gap = ts - self._prev_entry_ts if self._prev_entry_ts > 0 else 3.0
        duration = max(0.5, min(gap, 60.0))
        self._prev_entry_ts = ts
        now_mono = time.monotonic()

        # Sliding window: append the new utterance and evict anything older
        # than _TALK_WINDOW_SECONDS. This avoids the tumbling-window blind
        # spot where 4 min of speech at the end of one window plus 4 min at
        # the start of the next would never trigger a dominance alert.
        self._talk_events.append((now_mono, speaker, duration))
        self._trim_window(now_mono)

        # Filler ratio (host only)
        if self.host_speaker_name and speaker == self.host_speaker_name:
            self._filler_total += len(_FILLER_RE.findall(text))
            self._words_total += max(1, len(text.split()))

        # Monologue tracking (host only)
        if self.host_speaker_name and speaker == self.host_speaker_name:
            if self._last_speaker == speaker and self._monologue_run_start is not None:
                pass  # still going
            else:
                self._monologue_run_start = now_mono
        else:
            self._monologue_run_start = None

        self._last_speaker = speaker
        self._last_entry_mono = now_mono

        return self._maybe_emit(now_mono)

    def _trim_window(self, now_mono: float) -> None:
        """Evict talk events older than the sliding window."""
        cutoff = now_mono - _TALK_WINDOW_SECONDS
        while self._talk_events and self._talk_events[0][0] < cutoff:
            self._talk_events.popleft()

    def _window_totals(self, now_mono: float) -> tuple[dict[str, float], float]:
        """Return (per-speaker seconds, total seconds) over the sliding window."""
        self._trim_window(now_mono)
        per_speaker: dict[str, float] = {}
        for _, spkr, dur in self._talk_events:
            per_speaker[spkr] = per_speaker.get(spkr, 0.0) + dur
        return per_speaker, sum(per_speaker.values())

    def _maybe_emit(self, now_mono: float) -> list[dict]:
        tips: list[dict] = []
        if not self.host_speaker_name:
            return tips

        def _can_fire(metric: str) -> bool:
            if metric not in self.metrics:
                return False
            return now_mono - self._last_fired.get(metric, 0.0) >= self.nudge_interval

        host = self.host_speaker_name
        per_speaker, total = self._window_totals(now_mono)
        host_secs = per_speaker.get(host, 0.0)

        # ── talk_time ──
        if total >= 60 and _can_fire("talk_time"):
            host_pct = host_secs / total
            if host_pct > 0.70:
                tips.append({
                    "metric": "talk_time",
                    "message": f"You've spoken {round(host_pct * 100)}% of the last 5 minutes — invite others in.",
                    "severity": "warn",
                })
                self._last_fired["talk_time"] = now_mono
            elif host_pct < 0.10 and total > 180:
                tips.append({
                    "metric": "talk_time",
                    "message": "You've been quiet for a while — chime in or refocus the discussion.",
                    "severity": "info",
                })
                self._last_fired["talk_time"] = now_mono

        # ── monologue ──
        if (
            self._monologue_run_start is not None
            and now_mono - self._monologue_run_start > 90
            and _can_fire("monologue")
        ):
            tips.append({
                "metric": "monologue",
                "message": "You've been speaking non-stop for over 90 seconds — pause for input.",
                "severity": "warn",
            })
            self._last_fired["monologue"] = now_mono
            self._monologue_run_start = now_mono  # reset so we don't fire every second

        # ── filler_words ──
        if self._words_total >= 60 and _can_fire("filler_words"):
            ratio = self._filler_total / self._words_total
            if ratio > 0.10:
                tips.append({
                    "metric": "filler_words",
                    "message": f"Filler-word ratio is {round(ratio * 100)}% — consider slowing down.",
                    "severity": "info",
                })
                self._last_fired["filler_words"] = now_mono
            self._filler_total = 0
            self._words_total = 0

        # ── silence ──
        if _can_fire("silence") and now_mono - self._last_entry_mono > 25:
            tips.append({
                "metric": "silence",
                "message": "30 seconds of silence — consider asking a direct question.",
                "severity": "info",
            })
            self._last_fired["silence"] = now_mono

        return tips
