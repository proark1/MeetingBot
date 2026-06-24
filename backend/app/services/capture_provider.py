"""Capture-provider abstraction for meeting audio/transcript acquisition.

The rest of the product should not care whether meeting data came from the
local Playwright browser bot, a third-party capture API, a botless desktop
recorder, or an uploaded file. This module defines the small capture boundary
and adapts the existing Playwright/demo implementations to it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from app.config import settings
from app.store import BotSession


LiveEntryCallback = Callable[[dict[str, Any]], Awaitable[None]]
RuntimeReadyCallback = Callable[[Optional[dict[str, Any]]], Awaitable[None]]
AdmittedCallback = Callable[[], Awaitable[None]]


@dataclass
class CaptureCallbacks:
    """Callbacks the lifecycle layer exposes to a capture provider."""

    on_admitted: Optional[AdmittedCallback] = None
    on_live_entry: Optional[LiveEntryCallback] = None
    on_runtime_ready: Optional[RuntimeReadyCallback] = None
    external_leave_event: Optional[asyncio.Event] = None
    seen_chat_ids: set = field(default_factory=set)


@dataclass
class CaptureResult:
    """Provider-normalized capture result."""

    success: bool
    error: Optional[str] = None
    admitted: bool = False
    exit_reason: Optional[str] = None
    participants: list[str] = field(default_factory=list)
    live_transcript: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    is_demo_transcript: bool = False
    requires_transcription: bool = True


class CaptureProvider(Protocol):
    """Meeting-capture implementation contract."""

    name: str

    async def capture(
        self,
        *,
        bot: BotSession,
        audio_path: str,
        video_path: Optional[str],
        callbacks: CaptureCallbacks,
    ) -> CaptureResult:
        ...


class PlaywrightCaptureProvider:
    """Adapter around the existing local Playwright browser bot."""

    name = "playwright"

    async def capture(
        self,
        *,
        bot: BotSession,
        audio_path: str,
        video_path: Optional[str],
        callbacks: CaptureCallbacks,
    ) -> CaptureResult:
        from app.services.browser_bot import run_browser_bot

        raw = await run_browser_bot(
            meeting_url=bot.meeting_url,
            platform=bot.meeting_platform,
            bot_name=bot.bot_name or settings.BOT_NAME_DEFAULT,
            audio_path=audio_path,
            admission_timeout=settings.BOT_ADMISSION_TIMEOUT,
            max_duration=settings.BOT_MAX_DURATION,
            alone_timeout=settings.BOT_ALONE_TIMEOUT,
            on_admitted=callbacks.on_admitted,
            respond_on_mention=bot.respond_on_mention,
            mention_response_mode=bot.mention_response_mode,
            tts_provider=bot.tts_provider,
            start_muted=bot.start_muted,
            live_transcription=bot.live_transcription,
            on_live_transcript_entry=callbacks.on_live_entry,
            gemini_api_key=settings.GEMINI_API_KEY or "",
            record_video=bot.record_video,
            video_path=video_path,
            external_leave_event=callbacks.external_leave_event,
            consent_enabled=getattr(bot, "consent_enabled", False),
            consent_message=getattr(bot, "consent_message", None),
            on_runtime_ready=callbacks.on_runtime_ready,
            seen_chat_ids=callbacks.seen_chat_ids,
            bot_id=bot.id,
        )
        return CaptureResult(
            success=bool(raw.get("success")),
            error=raw.get("error"),
            admitted=bool(raw.get("admitted")),
            exit_reason=raw.get("exit_reason"),
            participants=list(raw.get("participants") or []),
            live_transcript=list(raw.get("live_transcript") or []),
            requires_transcription=True,
        )


class DemoCaptureProvider:
    """Provider for explicit unsupported-platform/sandbox demo capture."""

    name = "demo"

    async def capture(
        self,
        *,
        bot: BotSession,
        audio_path: str,
        video_path: Optional[str],
        callbacks: CaptureCallbacks,
    ) -> CaptureResult:
        from app.services import intelligence_service

        await asyncio.sleep(3)
        if callbacks.on_admitted is not None:
            await callbacks.on_admitted()
        await asyncio.sleep(settings.BOT_SIMULATION_DURATION)
        transcript = await intelligence_service.generate_demo_transcript(bot.meeting_url)
        return CaptureResult(
            success=True,
            admitted=True,
            exit_reason="demo_completed",
            transcript=transcript,
            is_demo_transcript=True,
            requires_transcription=False,
        )


def get_capture_provider(bot: BotSession, *, use_real_bot: bool) -> CaptureProvider:
    """Return the provider for this bot.

    ``CAPTURE_PROVIDER`` is intentionally conservative today: ``playwright`` is
    the only built-in real-meeting provider. The contract allows Recall.ai,
    Meeting BaaS, botless desktop capture, uploads, or platform-native imports
    to be added without changing the lifecycle pipeline.
    """

    if not use_real_bot:
        return DemoCaptureProvider()
    provider = (getattr(settings, "CAPTURE_PROVIDER", "playwright") or "playwright").lower()
    if provider != "playwright":
        raise RuntimeError(
            f"Capture provider {provider!r} is not installed. "
            "Available provider: 'playwright'."
        )
    return PlaywrightCaptureProvider()
