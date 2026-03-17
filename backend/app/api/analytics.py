"""Analytics and action-items aggregate endpoints."""

from fastapi import APIRouter, HTTPException, Query, Request

from app.deps import SUPERADMIN_ACCOUNT_ID
from app.store import store

router = APIRouter(tags=["Analytics"])

_ACTIVE_STATUSES = ("ready", "scheduled", "queued", "joining", "in_call", "call_ended")


@router.get("/analytics")
async def get_analytics(request: Request):
    """Aggregate analytics across all bots currently in memory (24-hour window)."""
    account_id = getattr(request.state, "account_id", None)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None
    all_bots, total = await store.list_bots(limit=10000, account_id=filter_account)

    by_status: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    durations: list[float] = []
    total_transcript_entries = 0
    total_tokens = 0
    total_cost = 0.0

    for bot in all_bots:
        by_status[bot.status] = by_status.get(bot.status, 0) + 1
        by_platform[bot.meeting_platform] = by_platform.get(bot.meeting_platform, 0) + 1
        if bot.duration_seconds is not None:
            durations.append(bot.duration_seconds)
        total_transcript_entries += len(bot.transcript)
        total_tokens += bot.ai_total_tokens
        total_cost += bot.ai_total_cost_usd

    done = by_status.get("done", 0)
    error = by_status.get("error", 0)
    finished = done + error
    success_rate = round(done / finished, 3) if finished > 0 else None
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None
    active = sum(by_status.get(s, 0) for s in _ACTIVE_STATUSES)

    return {
        "total_bots": total,
        "active_bots": active,
        "by_status": by_status,
        "by_platform": by_platform,
        "success_rate": success_rate,
        "avg_duration_seconds": avg_duration,
        "total_transcript_entries": total_transcript_entries,
        "total_ai_tokens": total_tokens,
        "total_ai_cost_usd": round(total_cost, 6),
    }


@router.get("/action-items/stats")
async def get_action_items_stats(request: Request):
    """Aggregate action-item statistics across all bots in memory."""
    account_id = getattr(request.state, "account_id", None)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None
    all_bots, _ = await store.list_bots(limit=10000, account_id=filter_account)

    items: list[dict] = []
    for bot in all_bots:
        if not bot.analysis:
            continue
        for item in bot.analysis.get("action_items", []):
            items.append({**item, "bot_id": bot.id, "meeting_url": bot.meeting_url})

    by_assignee: dict[str, int] = {}
    for item in items:
        assignee = item.get("assignee") or item.get("owner") or "Unassigned"
        by_assignee[assignee] = by_assignee.get(assignee, 0) + 1

    return {
        "total": len(items),
        "by_assignee": by_assignee,
        "recent": items[:20],
    }


@router.get("/search", tags=["Search"])
async def search_transcripts(
    q: str = Query(..., min_length=1, description="Search query — matched case-insensitively against transcript text."),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of matching snippets to return."),
    request: Request = None,
):
    """Full-text search across all transcripts in the 24-hour memory window.

    Returns up to `limit` transcript snippets whose text contains the query
    string (case-insensitive), each annotated with its source bot context.

    Results are ordered by bot creation time (newest first).
    """
    account_id = getattr(request.state, "account_id", None)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None
    all_bots, _ = await store.list_bots(limit=10000, account_id=filter_account)

    q_lower = q.lower()
    matches: list[dict] = []

    for bot in all_bots:
        for entry in bot.transcript:
            text = entry.get("text", "") or ""
            if q_lower in text.lower():
                matches.append({
                    "bot_id": bot.id,
                    "meeting_url": bot.meeting_url,
                    "meeting_platform": bot.meeting_platform,
                    "bot_status": bot.status,
                    "speaker": entry.get("speaker"),
                    "text": text,
                    "timestamp": entry.get("timestamp"),
                })
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break

    return {
        "query": q,
        "total": len(matches),
        "results": matches,
    }
