"""Tests for the capture-provider boundary."""

from app.store import BotSession


def _bot(platform="google_meet"):
    return BotSession(
        id="bot-test",
        meeting_url="https://meet.google.com/abc-defg-hij",
        meeting_platform=platform,
        bot_name="Test Bot",
        status="joining",
    )


async def test_playwright_capture_provider_normalizes_browser_result(monkeypatch):
    from app.services.capture_provider import CaptureCallbacks, PlaywrightCaptureProvider
    import app.services.browser_bot as browser_bot

    async def fake_run_browser_bot(**kwargs):
        assert kwargs["meeting_url"] == "https://meet.google.com/abc-defg-hij"
        assert kwargs["platform"] == "google_meet"
        return {
            "success": True,
            "admitted": True,
            "exit_reason": "ended",
            "participants": ["Alice"],
            "live_transcript": [{"speaker": "Alice", "text": "Hi", "timestamp": 1.0}],
        }

    monkeypatch.setattr(browser_bot, "run_browser_bot", fake_run_browser_bot)

    result = await PlaywrightCaptureProvider().capture(
        bot=_bot(),
        audio_path="/tmp/bot-test.wav",
        video_path=None,
        callbacks=CaptureCallbacks(),
    )

    assert result.success is True
    assert result.admitted is True
    assert result.requires_transcription is True
    assert result.participants == ["Alice"]
    assert result.live_transcript[0]["text"] == "Hi"


async def test_demo_capture_provider_returns_demo_transcript(monkeypatch):
    from app.services.capture_provider import CaptureCallbacks, DemoCaptureProvider
    from app.services import capture_provider as capture_mod
    from app.services import intelligence_service

    async def fake_sleep(_seconds):
        return None

    async def fake_demo(_url):
        return [{"speaker": "Demo", "text": "Synthetic note", "timestamp": 0.0}]

    admitted = {"value": False}

    async def on_admitted():
        admitted["value"] = True

    monkeypatch.setattr(capture_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(intelligence_service, "generate_demo_transcript", fake_demo)

    result = await DemoCaptureProvider().capture(
        bot=_bot(platform="whereby"),
        audio_path="/tmp/demo.wav",
        video_path=None,
        callbacks=CaptureCallbacks(on_admitted=on_admitted),
    )

    assert admitted["value"] is True
    assert result.success is True
    assert result.is_demo_transcript is True
    assert result.requires_transcription is False
    assert result.transcript[0]["text"] == "Synthetic note"
