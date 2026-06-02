"""Batched platform-analytics aggregation must equal a single pass.

platform_analytics streams BotSnapshot rows in batches and merges the per-batch
aggregation via _merge_agg (instead of holding every JSON blob in RAM). This
verifies that batched+merged aggregation is byte-for-byte identical to a single
pass over all rows, on synthetic snapshot rows.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api.admin import _aggregate_bot_snapshots, _merge_agg


def _row(i: int, *, status="done", platform_url="https://zoom.us/j/1",
         account="acct-a", days_ago=1):
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    data = {
        "analysis_mode": "full" if i % 2 else "transcript_only",
        "live_transcription": bool(i % 3),
        "template": "sales" if i % 4 == 0 else None,
        "duration_seconds": 60 + i,
        "transcription_provider": "gemini" if i % 2 else "whisper",
        "error_message": "boom" if status == "error" else None,
        "ai_usage": [
            {"model": "claude", "operation": "analysis",
             "total_tokens": 100 + i, "cost_usd": 0.01 * (i + 1)},
        ],
    }
    return SimpleNamespace(
        status=status, meeting_url=platform_url, created_at=created,
        account_id=account, data=json.dumps(data),
    )


def test_batched_aggregation_matches_single_pass():
    now = datetime.now(timezone.utc)
    d30, d7 = now - timedelta(days=30), now - timedelta(days=7)

    rows = []
    for i in range(53):
        rows.append(_row(
            i,
            status="error" if i % 7 == 0 else "done",
            platform_url="https://meet.google.com/x" if i % 2 else "https://zoom.us/j/1",
            account=f"acct-{i % 3}",
            days_ago=(i % 40),
        ))

    single = _aggregate_bot_snapshots(rows, d30, d7)

    # Batched: zero-init accumulator, merge each batch of 10.
    batched = _aggregate_bot_snapshots([], d30, d7)
    for start in range(0, len(rows), 10):
        part = _aggregate_bot_snapshots(rows[start:start + 10], d30, d7)
        _merge_agg(batched, part)

    # Normalise for comparison: sort sets/lists, and round floats (summation
    # order differs between single-pass and batched, so cost totals can differ
    # in the last binary digit — that's expected float non-associativity).
    def _r(x):
        if isinstance(x, float):
            return round(x, 6)
        if isinstance(x, dict):
            return {k: _r(v) for k, v in x.items()}
        if isinstance(x, (list, set)):
            return sorted(_r(v) for v in x)
        return x

    def _norm(agg):
        agg = dict(agg)
        agg["user_stats"] = {
            k: {**v, "features_used": sorted(v["features_used"])}
            for k, v in agg["user_stats"].items()
        }
        return _r(agg)

    assert _norm(single) == _norm(batched)
