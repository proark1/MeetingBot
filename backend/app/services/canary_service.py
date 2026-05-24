"""Synthetic canary for the meeting pipeline.

A canary periodically joins a known test meeting per platform and asserts the
end-to-end pipeline still works (admitted → audio flowing → captions/transcript
→ clean leave). It's the earliest warning when a platform ships a UI change that
breaks the DOM selectors in ``browser_bot.py`` — the one failure mode that the
unit suite can't catch because it needs a real meeting.

Design split:
  * ``evaluate_bot_outcome`` — the *brain* (what "healthy" means). Pure and
    fully unit-tested.
  * ``run_canary`` / ``_http_runner`` — the *I/O* (actually drive a bot). A
    black-box client against a deployed API: create a bot, poll to completion,
    read health signals. Can only run against a live deployment with a real
    test meeting, so it's injectable for testing.

Run via ``python scripts/canary.py`` (see that script for env config).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Terminal bot statuses — the canary waits until the bot reaches one of these.
_TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})
# exit_reason values that count as a healthy, intentional departure.
_HEALTHY_EXITS = frozenset({"ended", "alone_timeout", "max_duration", "leave_command"})


@dataclass
class CanaryConfig:
    """Per-run canary configuration."""

    base_url: str = ""
    api_key: str = ""
    # platform -> test meeting URL the bot should join
    meeting_urls: dict[str, str] = field(default_factory=dict)
    # health thresholds / requirements
    require_admitted: bool = True
    require_transcript: bool = True
    require_audio: bool = True
    require_participants: bool = False
    min_participants: int = 1
    min_audio_peak: int = 200          # 0..32767; matches the audio-health "silent" threshold
    poll_timeout_s: int = 600
    poll_interval_s: int = 10

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "CanaryConfig":
        """Build config from environment variables.

        Recognised vars: ``CANARY_BASE_URL``, ``CANARY_API_KEY``,
        ``CANARY_MEET_URL`` / ``CANARY_ZOOM_URL`` / ``CANARY_TEAMS_URL`` /
        ``CANARY_ONEPIZZA_URL``, plus ``CANARY_REQUIRE_*`` / ``CANARY_MIN_*``
        toggles.
        """
        env = env if env is not None else os.environ
        urls = {}
        for platform, var in (
            ("google_meet", "CANARY_MEET_URL"),
            ("zoom", "CANARY_ZOOM_URL"),
            ("microsoft_teams", "CANARY_TEAMS_URL"),
            ("onepizza", "CANARY_ONEPIZZA_URL"),
        ):
            url = (env.get(var) or "").strip()
            if url:
                urls[platform] = url

        def _bool(name: str, default: bool) -> bool:
            raw = env.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        def _int(name: str, default: int) -> int:
            try:
                return int(env.get(name, default))
            except (TypeError, ValueError):
                return default

        return cls(
            base_url=(env.get("CANARY_BASE_URL") or "").strip(),
            api_key=(env.get("CANARY_API_KEY") or "").strip(),
            meeting_urls=urls,
            require_admitted=_bool("CANARY_REQUIRE_ADMITTED", True),
            require_transcript=_bool("CANARY_REQUIRE_TRANSCRIPT", True),
            require_audio=_bool("CANARY_REQUIRE_AUDIO", True),
            require_participants=_bool("CANARY_REQUIRE_PARTICIPANTS", False),
            min_participants=_int("CANARY_MIN_PARTICIPANTS", 1),
            min_audio_peak=_int("CANARY_MIN_AUDIO_PEAK", 200),
            poll_timeout_s=_int("CANARY_POLL_TIMEOUT_S", 600),
            poll_interval_s=_int("CANARY_POLL_INTERVAL_S", 10),
        )


@dataclass
class CanaryReport:
    """Result of evaluating one canary run."""

    platform: str
    ok: bool
    checks: dict[str, bool]
    details: dict[str, str]

    def summary(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        failed = [name for name, passed in self.checks.items() if not passed]
        tail = "" if self.ok else f" (failed: {', '.join(failed)})"
        return f"[{status}] canary {self.platform}{tail}"


def evaluate_bot_outcome(platform: str, outcome: dict, cfg: CanaryConfig) -> CanaryReport:
    """Evaluate a completed bot's health signals against the canary's requirements.

    ``outcome`` is a best-effort dict of signals collected from the API:
    ``admitted`` (bool), ``status`` (str), ``exit_reason`` (str),
    ``transcript_len`` (int), ``participants_count`` (int),
    ``audio_peak`` (int|None), ``error`` (str|None).
    Pure function — no I/O — so it's fully unit-testable.
    """
    checks: dict[str, bool] = {}
    details: dict[str, str] = {}

    # Reached a terminal state without erroring.
    status = outcome.get("status")
    error = outcome.get("error")
    checks["completed"] = status in _TERMINAL_STATUSES and status != "error" and not error
    details["completed"] = f"status={status!r} error={error!r}"

    if cfg.require_admitted:
        checks["admitted"] = bool(outcome.get("admitted"))
        details["admitted"] = f"admitted={outcome.get('admitted')!r}"

    # exit_reason should be an intentional departure, not a crash.
    exit_reason = outcome.get("exit_reason")
    checks["clean_exit"] = exit_reason in _HEALTHY_EXITS
    details["clean_exit"] = f"exit_reason={exit_reason!r}"

    if cfg.require_transcript:
        tlen = int(outcome.get("transcript_len") or 0)
        checks["transcript"] = tlen > 0
        details["transcript"] = f"transcript_len={tlen}"

    if cfg.require_audio:
        peak = outcome.get("audio_peak")
        checks["audio"] = peak is not None and int(peak) >= cfg.min_audio_peak
        details["audio"] = f"audio_peak={peak} (min {cfg.min_audio_peak})"

    if cfg.require_participants:
        pc = int(outcome.get("participants_count") or 0)
        checks["participants"] = pc >= cfg.min_participants
        details["participants"] = f"participants_count={pc} (min {cfg.min_participants})"

    return CanaryReport(
        platform=platform,
        ok=all(checks.values()),
        checks=checks,
        details=details,
    )


# Signature of a runner: given (platform, url, cfg) drive a bot and return the
# outcome signal dict. Injectable so tests can supply a fake.
Runner = Callable[[str, str, CanaryConfig], Awaitable[dict]]


async def _http_runner(platform: str, url: str, cfg: CanaryConfig) -> dict:
    """Black-box runner: create a bot via the deployed API, poll to completion,
    and read its health signals. Requires a reachable API + real test meeting.
    """
    import asyncio

    import httpx

    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    async with httpx.AsyncClient(base_url=cfg.base_url, headers=headers, timeout=30.0) as client:
        resp = await client.post("/api/v1/bot", json={
            "meeting_url": url,
            "bot_name": "JustHereToListen.io canary",
            "live_transcription": True,
        })
        resp.raise_for_status()
        bot_id = resp.json()["id"]

        deadline = asyncio.get_event_loop().time() + cfg.poll_timeout_s
        bot: dict = {}
        while asyncio.get_event_loop().time() < deadline:
            r = await client.get(f"/api/v1/bot/{bot_id}")
            r.raise_for_status()
            bot = r.json()
            if bot.get("status") in _TERMINAL_STATUSES:
                break
            await asyncio.sleep(cfg.poll_interval_s)

        # Best-effort health signals from the debug endpoint (may not exist).
        audio_peak = None
        try:
            dbg = await client.get(f"/api/v1/bot/{bot_id}/debug")
            if dbg.status_code == 200:
                samples = dbg.json().get("audio_health_samples", []) or []
                peaks = [s.get("peak_recent_3s") for s in samples if s.get("peak_recent_3s") is not None]
                audio_peak = max(peaks) if peaks else None
        except Exception:
            pass

        return {
            "status": bot.get("status"),
            "admitted": bool(bot.get("admitted", bot.get("status") in _TERMINAL_STATUSES and bot.get("status") != "error")),
            "exit_reason": bot.get("exit_reason"),
            "transcript_len": len(bot.get("transcript") or []),
            "participants_count": len(bot.get("participants") or []),
            "audio_peak": audio_peak,
            "error": bot.get("error_message") or bot.get("error"),
        }


async def run_canary(platform: str, url: str, cfg: CanaryConfig, runner: Optional[Runner] = None) -> CanaryReport:
    """Drive one canary run for a platform and evaluate the result."""
    runner = runner or _http_runner
    try:
        outcome = await runner(platform, url, cfg)
    except Exception as exc:
        logger.warning("Canary %s failed to run: %s", platform, exc)
        return CanaryReport(
            platform=platform, ok=False,
            checks={"ran": False},
            details={"ran": f"runner raised: {exc}"},
        )
    report = evaluate_bot_outcome(platform, outcome, cfg)
    logger.info("%s — %s", report.summary(), report.details)
    return report


async def run_all(cfg: CanaryConfig, runner: Optional[Runner] = None) -> list[CanaryReport]:
    """Run the canary for every configured platform."""
    reports = []
    for platform, url in cfg.meeting_urls.items():
        reports.append(await run_canary(platform, url, cfg, runner=runner))
    return reports
