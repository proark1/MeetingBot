"""Unit tests for admin._aggregate_bot_snapshots (extracted for off-loop run)."""

import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from app.api.admin import _aggregate_bot_snapshots


def _snap(status, url, created_at, account_id, data):
    return SimpleNamespace(
        status=status, meeting_url=url, created_at=created_at,
        account_id=account_id, data=json.dumps(data) if data is not None else None,
    )


def test_aggregate_rolls_up_status_platform_features_and_ai():
    now = datetime.now(timezone.utc)
    d30, d7 = now - timedelta(days=30), now - timedelta(days=7)
    snaps = [
        _snap("done", "https://zoom.us/j/1", now - timedelta(days=1), "acct-1", {
            "analysis_mode": "full", "live_transcription": True, "template": "sales",
            "ai_usage": [{"model": "claude-sonnet-4-6", "operation": "analysis",
                          "total_tokens": 100, "cost_usd": 0.5}],
        }),
        _snap("error", "https://meet.google.com/abc", now - timedelta(days=2), "acct-1", {
            "analysis_mode": "transcript_only", "error_message": "join failed",
            "ai_usage": [],
        }),
        _snap("done", "https://teams.microsoft.com/x", now - timedelta(days=40), "acct-2", {
            "ai_usage": [{"model": "claude-sonnet-4-6", "operation": "chapters",
                          "total_tokens": 50, "cost_usd": 0.25}],
        }),
    ]
    agg = _aggregate_bot_snapshots(snaps, d30, d7)

    assert agg["status_counts"] == {"done": 2, "error": 1}
    assert agg["total_ai_tokens"] == 150
    assert round(agg["total_ai_cost"], 2) == 0.75
    assert agg["features"]["analysis_full"] == 2  # snap3 has no mode -> defaults to "full"
    assert agg["features"]["analysis_transcript_only"] == 1
    assert agg["features"]["live_transcription"] == 1
    assert agg["features"]["custom_template"] == 1
    assert agg["template_counts"]["sales"] == 1
    assert agg["error_messages"]["join failed"] == 1
    # per-model AI bucket
    assert agg["ai_by_model"]["claude-sonnet-4-6"]["tokens"] == 150
    assert agg["ai_by_model"]["claude-sonnet-4-6"]["calls"] == 2
    # per-user aggregate
    assert agg["user_stats"]["acct-1"]["total_bots"] == 2
    assert agg["user_stats"]["acct-1"]["ai_tokens"] == 100


def test_aggregate_handles_bad_json_blob():
    now = datetime.now(timezone.utc)
    bad = SimpleNamespace(status="done", meeting_url="https://zoom.us/j/9",
                          created_at=now, account_id="a", data="{not json")
    agg = _aggregate_bot_snapshots([bad], now - timedelta(days=30), now - timedelta(days=7))
    assert agg["status_counts"] == {"done": 1}
    assert agg["total_ai_tokens"] == 0


def test_aggregate_empty():
    now = datetime.now(timezone.utc)
    agg = _aggregate_bot_snapshots([], now, now)
    assert agg["status_counts"] == {} and agg["total_ai_cost"] == 0.0
