"""Round-trip tests for BotSession.to_state_dict / from_state_dict."""

import asyncio
import json
from datetime import datetime, timezone

from app.store import BotSession


def _bot(**over):
    base = dict(
        id="b1", meeting_url="https://meet.google.com/x",
        meeting_platform="google_meet", bot_name="b", status="in_call",
    )
    base.update(over)
    return BotSession(**base)


def test_round_trip_preserves_core_and_datetimes():
    bot = _bot(
        account_id="acct-1",
        transcript=[{"speaker": "A", "text": "hi", "timestamp": 1.0}],
        analysis={"summary": "s"},
        transcription_failed=True,
        transcription_failure_reason="Transcription returned no content",
        created_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        started_at=datetime(2026, 5, 1, 12, 1, tzinfo=timezone.utc),
    )
    d = bot.to_state_dict()
    # JSON-serializable
    s = json.dumps(d)
    restored = BotSession.from_state_dict(json.loads(s))

    assert restored.id == "b1"
    assert restored.account_id == "acct-1"
    assert restored.transcript == bot.transcript
    assert restored.analysis == {"summary": "s"}
    assert restored.transcription_failed is True
    assert restored.transcription_failure_reason == "Transcription returned no content"
    assert restored.created_at == bot.created_at
    assert isinstance(restored.created_at, datetime)
    assert restored.started_at == bot.started_at


def test_non_serializable_fields_excluded():
    bot = _bot()
    bot.runtime = {"page": object()}             # live Playwright handle
    bot.leave_event = asyncio.Event()
    bot.seen_chat_ids = {"hash1", "hash2"}

    d = bot.to_state_dict()
    assert "runtime" not in d
    assert "leave_event" not in d
    assert "seen_chat_ids" not in d

    # Reconstruction falls back to defaults for the excluded handles.
    restored = BotSession.from_state_dict(d)
    assert restored.runtime is None
    assert restored.leave_event is None
    assert restored.seen_chat_ids == set()


def test_from_state_dict_ignores_unknown_keys():
    d = _bot().to_state_dict()
    d["some_future_field"] = "ignored"
    restored = BotSession.from_state_dict(d)  # must not raise
    assert restored.id == "b1"


def test_naive_datetime_round_trips():
    bot = _bot(created_at=datetime(2026, 5, 1, 12, 0))  # naive
    restored = BotSession.from_state_dict(bot.to_state_dict())
    assert restored.created_at == datetime(2026, 5, 1, 12, 0)
