"""Regression tests for transcription failure cleanup behavior."""

import uuid

import pytest

from app.store import BotSession, store


@pytest.mark.asyncio
async def test_analysis_cleanup_skips_transcription_after_recorded_failure(monkeypatch):
    """A known no-content transcription failure should not trigger a second provider call."""
    from app.services import bot_service

    bot = BotSession(
        id=f"bot-{uuid.uuid4().hex}",
        meeting_url="https://meet.google.com/abc-defg-hij",
        meeting_platform="google_meet",
        bot_name="JustHereToListen.io",
        status="transcribing",
        transcription_failed=True,
        transcription_failure_reason="Transcription returned no content",
    )

    async def _should_not_transcribe(*args, **kwargs):
        raise AssertionError("transcription should not be retried")

    async def _noop_dispatch(*args, **kwargs):
        return None

    monkeypatch.setattr(bot_service, "_transcribe_audio_for_bot", _should_not_transcribe)
    monkeypatch.setattr(bot_service.webhook_service, "dispatch_event", _noop_dispatch)

    await store.create_bot(bot)
    try:
        await bot_service._do_analysis_inner(bot, "/tmp/nonexistent-meeting-audio.wav", use_real_bot=True)
        saved = await store.get_bot(bot.id)
        assert saved is not None
        assert saved.transcript == []
        assert saved.transcription_failed is True
        assert saved.transcription_failure_reason == "Transcription returned no content"
    finally:
        await store.delete_bot(bot.id)
