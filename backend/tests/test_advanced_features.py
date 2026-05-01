"""Tests for the opt-in advanced bot features (#5, #7, #8, #11, #13, #15).

These tests cover:
  - Schema defaults (every advanced feature off out of the box)
  - Schema activation (turning each feature on with its nested config)
  - Service-level pure-logic helpers (decision detector, chat-QA trigger,
    speaker analytics aggregator, coaching engine)
  - API endpoint guards (404 vs 409 when feature is disabled)
  - MCP tool registration

Live-meeting paths (PlayWright + TTS + chat scraping) are NOT exercised here;
they require a real meeting URL and are covered by manual smoke tests.
"""

import asyncio

import httpx
import pytest


# ── Schema ──────────────────────────────────────────────────────────────────

def test_schema_defaults_all_features_off():
    from app.schemas.bot import BotCreate

    payload = BotCreate(meeting_url="https://meet.google.com/abc-defg-hij")
    assert payload.enable_chat_qa is False
    assert payload.enable_speaker_analytics is False
    assert payload.enable_decision_detection is False
    assert payload.enable_cross_meeting_memory is False
    assert payload.enable_coaching is False
    assert payload.agentic_autonomy == "off"
    assert payload.agentic_instructions == []
    # Nested configs default to None — only constructed when feature is on.
    assert payload.chat_qa is None
    assert payload.coaching is None
    assert payload.speaker_analytics is None
    assert payload.cross_meeting_memory is None


def test_schema_activate_all_features():
    from app.schemas.bot import (
        BotCreate, AgenticInstruction, ChatQaConfig, CoachingConfig,
        SpeakerAnalyticsConfig, CrossMeetingMemoryConfig,
    )

    payload = BotCreate(
        meeting_url="https://meet.google.com/abc-defg-hij",
        enable_chat_qa=True,
        chat_qa=ChatQaConfig(trigger="@assistant", reply_via="both", rate_limit_seconds=20),
        enable_coaching=True,
        coaching=CoachingConfig(metrics=["talk_time", "filler_words"], host_speaker_name="Alice"),
        enable_speaker_analytics=True,
        speaker_analytics=SpeakerAnalyticsConfig(interval_seconds=15, include_sentiment=True),
        enable_decision_detection=True,
        enable_cross_meeting_memory=True,
        cross_meeting_memory=CrossMeetingMemoryConfig(lookback_days=7, max_meetings=2),
        agentic_autonomy="medium",
        agentic_instructions=[
            AgenticInstruction(instruction="Push back on scope creep", trigger="on_topic", speak=True, max_invocations=3),
        ],
    )
    assert payload.enable_chat_qa
    assert payload.chat_qa.trigger == "@assistant"
    assert payload.coaching.metrics == ["talk_time", "filler_words"]
    assert payload.speaker_analytics.interval_seconds == 15
    assert payload.cross_meeting_memory.lookback_days == 7
    assert payload.agentic_autonomy == "medium"
    assert payload.agentic_instructions[0].speak is True


def test_schema_agentic_caps_at_20():
    from app.schemas.bot import BotCreate, AgenticInstruction

    too_many = [AgenticInstruction(instruction=f"do thing {i}") for i in range(21)]
    with pytest.raises(Exception):
        BotCreate(
            meeting_url="https://meet.google.com/abc-defg-hij",
            agentic_instructions=too_many,
        )


# ── Decision detector ───────────────────────────────────────────────────────

def test_decision_detector_action_phrase():
    from app.services import decision_detector

    out = decision_detector.detect({
        "text": "OK, I will send the deck by Friday morning",
        "speaker": "Alice", "timestamp": 12.0,
    })
    kinds = [r["kind"] for r in out]
    assert "action" in kinds


def test_decision_detector_decision_phrase():
    from app.services import decision_detector

    out = decision_detector.detect({
        "text": "Great — we have decided to ship the v2 release Thursday",
        "speaker": "Bob", "timestamp": 30.0,
    })
    kinds = [r["kind"] for r in out]
    assert "decision" in kinds


def test_decision_detector_filler_does_not_match():
    from app.services import decision_detector

    out = decision_detector.detect({"text": "um yeah maybe", "speaker": "Alice", "timestamp": 1.0})
    assert out == []


def test_decision_detector_dedup_kinds_per_entry():
    from app.services import decision_detector

    # Two action patterns in the same line — should still emit only one action record.
    out = decision_detector.detect({
        "text": "I will send the doc and I'll also follow up next week",
        "speaker": "Alice", "timestamp": 5.0,
    })
    actions = [r for r in out if r["kind"] == "action"]
    assert len(actions) == 1


# ── Chat-QA trigger parser ──────────────────────────────────────────────────

def test_chat_qa_matches_trigger():
    from app.services.chat_qa_service import matches_trigger

    assert matches_trigger("@bot what did we decide?", "@bot") == "what did we decide?"
    assert matches_trigger("@Bot, summarise this", "@bot") == "summarise this"
    assert matches_trigger("@bot:   recap please ", "@bot") == "recap please"
    assert matches_trigger("hello world", "@bot") is None
    assert matches_trigger("", "@bot") is None
    assert matches_trigger("@bot", "@bot") is None  # trigger only, no question


# ── Speaker analytics aggregator ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_speaker_analytics_aggregator_basic():
    from app.services.speaker_analytics_service import SpeakerAnalyticsAggregator

    agg = SpeakerAnalyticsAggregator(bot_id="t", account_id=None, interval_seconds=999)
    entries = [
        {"speaker": "Alice", "text": "Hello like um actually", "timestamp": 1.0, "source": "voice"},
        {"speaker": "Alice", "text": "I think we should ship", "timestamp": 6.0, "source": "voice"},
        {"speaker": "Bob",   "text": "Yes agreed",             "timestamp": 7.0, "source": "voice"},
        {"speaker": "Alice", "text": "Right so basically",     "timestamp": 20.0, "source": "voice"},
    ]
    for e in entries:
        await agg.feed(e)
    snap = agg.snapshot()
    assert snap["bot_id"] == "t"
    assert len(snap["speakers"]) == 2
    bob = next(s for s in snap["speakers"] if s["speaker"] == "Bob")
    # Bob spoke within 1.5s of Alice → counted as an interruption.
    assert bob["interruptions"] == 1
    alice = next(s for s in snap["speakers"] if s["speaker"] == "Alice")
    assert alice["filler_words"] >= 3


@pytest.mark.asyncio
async def test_speaker_analytics_skips_chat_entries():
    from app.services.speaker_analytics_service import SpeakerAnalyticsAggregator

    agg = SpeakerAnalyticsAggregator(bot_id="t", account_id=None, interval_seconds=999)
    snap = await agg.feed({"speaker": "Alice", "text": "ignored", "timestamp": 5.0, "source": "chat"})
    assert snap is None
    assert agg.snapshot()["speakers"] == []


# ── Coaching engine ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_coaching_engine_dominance_alert():
    from app.services.coaching_service import CoachingEngine

    eng = CoachingEngine(
        bot_id="t", account_id=None,
        host_speaker_name="Alice",
        nudge_interval_seconds=30,
    )
    # Make Alice dominate
    tips_seen: list[dict] = []
    for i in range(20):
        out = await eng.feed({"speaker": "Alice", "text": "blah blah blah", "timestamp": i * 5.0})
        tips_seen.extend(out)
    # At least one tip should reference talk_time or monologue
    metrics = {t["metric"] for t in tips_seen}
    assert metrics & {"talk_time", "monologue"}


@pytest.mark.asyncio
async def test_coaching_engine_respects_metric_filter():
    from app.services.coaching_service import CoachingEngine

    # Only ask for filler_words — talk_time tips should never appear.
    eng = CoachingEngine(
        bot_id="t", account_id=None,
        host_speaker_name="Alice",
        metrics=["filler_words"],
        nudge_interval_seconds=30,
    )
    tips_seen: list[dict] = []
    for i in range(20):
        out = await eng.feed({"speaker": "Alice", "text": "um like uh actually basically", "timestamp": i * 3.0})
        tips_seen.extend(out)
    assert all(t["metric"] == "filler_words" for t in tips_seen)


# ── Agentic engine — autonomy gating ────────────────────────────────────────

@pytest.mark.asyncio
async def test_agentic_engine_off_does_nothing():
    from app.services.agentic_service import AgenticEngine

    class _Bot:
        agentic_autonomy = "off"
        agentic_instructions = [
            {"instruction": "Push back if scope creeps", "trigger": "on_topic"},
        ]
        agentic_invocations = {}
        transcript = []

    eng = AgenticEngine(_Bot())
    out = await eng.feed({"speaker": "Alice", "text": "scope creep", "timestamp": 1.0})
    assert out == []


@pytest.mark.asyncio
async def test_agentic_engine_low_only_manual():
    from app.services.agentic_service import AgenticEngine

    class _Bot:
        agentic_autonomy = "low"
        agentic_instructions = [
            {"instruction": "Push back if scope creeps", "trigger": "on_topic"},
            {"instruction": "Anything", "trigger": "manual"},
        ]
        agentic_invocations = {}
        transcript = []

    eng = AgenticEngine(_Bot())
    out = await eng.feed({"speaker": "Alice", "text": "anything", "timestamp": 1.0})
    # Even though on_topic instruction matches, autonomy=low blocks it.
    assert out == []


# ── End-to-end API surface ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_bot_with_all_features_returns_echoed_flags(auth_client: httpx.AsyncClient):
    """Creating a bot with every advanced feature on should echo the flags back."""
    resp = await auth_client.post(
        "/api/v1/bot",
        json={
            "meeting_url": "https://zoom.us/j/8888888888",
            "bot_name": "Advanced Bot",
            "enable_chat_qa": True,
            "chat_qa": {"trigger": "@bot", "reply_via": "chat", "rate_limit_seconds": 5},
            "enable_speaker_analytics": True,
            "speaker_analytics": {"interval_seconds": 30},
            "enable_decision_detection": True,
            "enable_cross_meeting_memory": True,
            "cross_meeting_memory": {"lookback_days": 14, "max_meetings": 3},
            "enable_coaching": True,
            "coaching": {"metrics": ["talk_time", "filler_words"]},
            "agentic_autonomy": "medium",
            "agentic_instructions": [
                {"instruction": "Push back on scope", "trigger": "on_topic", "speak": True},
            ],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    assert data["enable_chat_qa"] is True
    assert data["enable_speaker_analytics"] is True
    assert data["enable_decision_detection"] is True
    assert data["enable_cross_meeting_memory"] is True
    assert data["enable_coaching"] is True
    assert data["agentic_autonomy"] == "medium"


@pytest.mark.asyncio
async def test_decisions_endpoint_requires_feature(auth_client: httpx.AsyncClient):
    """GET /decisions on a bot without the flag set should 409, not crash."""
    create = await auth_client.post(
        "/api/v1/bot",
        json={"meeting_url": "https://zoom.us/j/7777777777"},
    )
    bot_id = create.json()["id"]
    resp = await auth_client.get(f"/api/v1/bot/{bot_id}/decisions")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_coaching_endpoint_requires_feature(auth_client: httpx.AsyncClient):
    create = await auth_client.post(
        "/api/v1/bot",
        json={"meeting_url": "https://zoom.us/j/6666666666"},
    )
    bot_id = create.json()["id"]
    resp = await auth_client.get(f"/api/v1/bot/{bot_id}/coaching/tips")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_agentic_get_endpoint_returns_defaults(auth_client: httpx.AsyncClient):
    create = await auth_client.post(
        "/api/v1/bot",
        json={"meeting_url": "https://zoom.us/j/5555555555"},
    )
    bot_id = create.json()["id"]
    resp = await auth_client.get(f"/api/v1/bot/{bot_id}/agentic/instructions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["autonomy"] == "off"
    assert data["instructions"] == []


@pytest.mark.asyncio
async def test_agentic_put_updates_instructions(auth_client: httpx.AsyncClient):
    create = await auth_client.post(
        "/api/v1/bot",
        json={
            "meeting_url": "https://zoom.us/j/4444444444",
            "agentic_autonomy": "medium",
            "agentic_instructions": [
                {"instruction": "old", "trigger": "manual"},
            ],
        },
    )
    bot_id = create.json()["id"]
    new = await auth_client.put(
        f"/api/v1/bot/{bot_id}/agentic/instructions",
        json={
            "instructions": [
                {"instruction": "new task", "trigger": "on_topic", "speak": False},
            ],
            "autonomy": "high",
        },
    )
    assert new.status_code == 200
    body = new.json()
    assert body["agentic_instructions"][0]["instruction"] == "new task"
    assert body["agentic_autonomy"] == "high"


@pytest.mark.asyncio
async def test_chat_qa_ask_endpoint_works_without_flag(auth_client: httpx.AsyncClient):
    """The manual /chat-qa/ask endpoint is available even when enable_chat_qa is off."""
    create = await auth_client.post(
        "/api/v1/bot",
        json={"meeting_url": "https://zoom.us/j/3333333333"},
    )
    bot_id = create.json()["id"]
    resp = await auth_client.post(
        f"/api/v1/bot/{bot_id}/chat-qa/ask",
        json={"question": "What did we decide?"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["bot_id"] == bot_id
    assert "answer" in data


# ── Webhook event registration ──────────────────────────────────────────────

def test_new_webhook_events_registered():
    from app.api.webhooks import WEBHOOK_EVENTS
    for evt in (
        "bot.decision_detected",
        "bot.coaching_tip",
        "bot.speaker_analytics",
        "bot.agentic_action",
    ):
        assert evt in WEBHOOK_EVENTS, f"Missing webhook event: {evt}"


# ── MCP tool registration ───────────────────────────────────────────────────

def test_new_mcp_tools_registered():
    from app.services.mcp_service import MCP_SERVER_MANIFEST, _TOOL_HANDLERS
    tool_names = {t["name"] for t in MCP_SERVER_MANIFEST["tools"]}
    expected = {
        "get_decisions",
        "get_live_analytics",
        "get_coaching_tips",
        "get_related_meetings",
        "set_agentic_instructions",
        "trigger_agentic_instruction",
        "ask_chat_qa",
    }
    missing_manifest = expected - tool_names
    missing_handler = expected - set(_TOOL_HANDLERS.keys())
    assert not missing_manifest, f"Missing in manifest: {missing_manifest}"
    assert not missing_handler, f"Missing handler: {missing_handler}"
