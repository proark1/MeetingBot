"""Analytics and action-items aggregate endpoints."""

import json
import logging
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.deps import SUPERADMIN_ACCOUNT_ID
from app.store import store

logger = logging.getLogger(__name__)
router = APIRouter()

_ACTIVE_STATUSES = ("ready", "scheduled", "queued", "joining", "in_call", "call_ended")

# Module-level result caches: cache_key → (monotonic_ts, result)
_analytics_cache: dict[str, tuple[float, dict]] = {}
_api_usage_cache: dict[str, tuple[float, dict]] = {}
_ANALYTICS_TTL = 30.0
_API_USAGE_TTL = 60.0

# Shared bot list cache — all analytics endpoints draw from this single fetch per 30s
_bots_list_cache: dict[str, tuple[float, list]] = {}
_BOTS_LIST_TTL = 30.0


def _cache_get(cache: dict, key: str, ttl: float):
    ts, val = cache.get(key, (0.0, None))
    if val is not None and (_time.monotonic() - ts) < ttl:
        return val
    return None


def _cache_set(cache: dict, key: str, value: dict) -> None:
    cache[key] = (_time.monotonic(), value)
    if len(cache) > 200:
        oldest = min(cache, key=lambda k: cache[k][0])
        del cache[oldest]


async def _get_bots(filter_account: Optional[str]) -> list:
    """Fetch (or return cached) bot list for analytics — shared across all endpoints."""
    key = filter_account or "__all__"
    ts, bots = _bots_list_cache.get(key, (0.0, None))
    if bots is not None and (_time.monotonic() - ts) < _BOTS_LIST_TTL:
        return bots
    all_bots, _ = await store.list_bots(limit=10000, account_id=filter_account)
    _bots_list_cache[key] = (_time.monotonic(), all_bots)
    if len(_bots_list_cache) > 200:
        oldest = min(_bots_list_cache, key=lambda k: _bots_list_cache[k][0])
        del _bots_list_cache[oldest]
    return all_bots


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/analytics", tags=["Analytics"])
async def get_analytics(request: Request):
    """Aggregate analytics across all bots currently in memory (24-hour window)."""
    account_id = getattr(request.state, "account_id", None)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None

    cache_key = f"analytics:{filter_account}"
    cached = _cache_get(_analytics_cache, cache_key, _ANALYTICS_TTL)
    if cached is not None:
        return cached

    all_bots = await _get_bots(filter_account)
    total = len(all_bots)

    by_status: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    by_workspace: dict[str, int] = {}
    durations: list[float] = []
    health_scores: list[int] = []
    total_transcript_entries = 0
    total_tokens = 0
    total_cost = 0.0
    total_meeting_cost = 0.0
    meetings_with_quiet_participants = 0
    seven_days_ago = _now() - timedelta(days=7)
    meetings_last_7d = 0

    for bot in all_bots:
        by_status[bot.status] = by_status.get(bot.status, 0) + 1
        by_platform[bot.meeting_platform] = by_platform.get(bot.meeting_platform, 0) + 1
        if bot.duration_seconds is not None:
            durations.append(bot.duration_seconds)
        total_transcript_entries += len(bot.transcript)
        total_tokens += bot.ai_total_tokens
        total_cost += bot.ai_total_cost_usd

        # New aggregations
        hs = getattr(bot, "health_score", None)
        if hs is not None:
            health_scores.append(hs)
        mc = getattr(bot, "meeting_cost_usd", None)
        if mc:
            total_meeting_cost += mc
        if bot.workspace_id:
            by_workspace[bot.workspace_id] = by_workspace.get(bot.workspace_id, 0) + 1
        if bot.speaker_stats:
            if any(s.get("is_quiet") for s in bot.speaker_stats):
                meetings_with_quiet_participants += 1
        if bot.created_at and bot.created_at >= seven_days_ago:
            meetings_last_7d += 1

    done = by_status.get("done", 0)
    error = by_status.get("error", 0)
    finished = done + error
    success_rate = round(done / finished, 3) if finished > 0 else None
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None
    avg_health_score = round(sum(health_scores) / len(health_scores), 1) if health_scores else None
    active = sum(by_status.get(s, 0) for s in _ACTIVE_STATUSES)
    quiet_participant_rate = round(meetings_with_quiet_participants / finished, 3) if finished > 0 else None

    # Top 5 workspaces by bot count
    top_workspaces = sorted(by_workspace.items(), key=lambda x: x[1], reverse=True)[:5]

    result = {
        "total_bots": total,
        "active_bots": active,
        "by_status": by_status,
        "by_platform": by_platform,
        "success_rate": success_rate,
        "avg_duration_seconds": avg_duration,
        "total_transcript_entries": total_transcript_entries,
        "total_ai_tokens": total_tokens,
        "total_ai_cost_usd": round(total_cost, 6),
        # New metrics
        "avg_health_score": avg_health_score,
        "total_meeting_cost_usd": round(total_meeting_cost, 2) if total_meeting_cost else None,
        "meetings_per_week": round(meetings_last_7d / 7, 1),
        "quiet_participant_rate": quiet_participant_rate,
        "top_workspaces": [{"workspace_id": wid, "count": cnt} for wid, cnt in top_workspaces],
    }
    _cache_set(_analytics_cache, cache_key, result)
    return result


@router.get("/action-items/stats", tags=["Analytics"])
async def get_action_items_stats(request: Request):
    """Aggregate action-item statistics across all bots in memory."""
    account_id = getattr(request.state, "account_id", None)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None
    all_bots = await _get_bots(filter_account)

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


@router.get("/analytics/recurring", tags=["Analytics"])
async def get_recurring_insights(
    attendees: str = Query(None, description="Comma-separated attendee names (e.g. 'Alice,Bob'). Used to group recurring meetings by participant set."),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of past meetings to include in the trend."),
    request: Request = None,
):
    """Recurring meeting trend analysis for a fixed group of attendees.

    Finds meetings in the 24-hour in-memory window (and DB archive) that share
    the same set of participants and returns a time-series of quality metrics.

    Optionally pass ``attendees=Alice,Bob`` to filter by participant name; omit
    to return insights across ALL recurring meeting groups.
    """
    account_id = getattr(request.state, "account_id", None)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None

    target_names: set[str] | None = None
    if attendees:
        target_names = {n.strip().lower() for n in attendees.split(",") if n.strip()}

    all_bots = await _get_bots(filter_account)
    done_bots = [b for b in all_bots if b.status in ("done", "cancelled") and b.transcript]

    # Filter by attendees if provided
    if target_names:
        filtered = []
        for bot in done_bots:
            bot_speakers = {e.get("speaker", "").strip().lower() for e in bot.transcript}
            if target_names.issubset(bot_speakers):
                filtered.append(bot)
        done_bots = filtered

    # Sort by creation time descending, take the most recent `limit` bots
    done_bots = sorted(done_bots, key=lambda b: b.created_at, reverse=True)[:limit]

    series = []
    for bot in done_bots:
        action_items = []
        if bot.analysis:
            action_items = bot.analysis.get("action_items", [])
        series.append({
            "bot_id": bot.id,
            "date": bot.created_at.isoformat(),
            "duration_s": bot.duration_seconds,
            "health_score": getattr(bot, "health_score", None),
            "action_item_count": len(action_items),
            "sentiment": bot.analysis.get("sentiment") if bot.analysis else None,
            "participants": bot.participants,
            "meeting_cost_usd": getattr(bot, "meeting_cost_usd", None),
        })

    # Simple trend computation on the time series (oldest → newest)
    rev = list(reversed(series))
    health_trend = "stable"
    if len(rev) >= 3:
        recent_hs = [r["health_score"] for r in rev[-3:] if r["health_score"] is not None]
        older_hs  = [r["health_score"] for r in rev[:max(1, len(rev)-3)] if r["health_score"] is not None]
        if recent_hs and older_hs:
            if sum(recent_hs) / len(recent_hs) > sum(older_hs) / len(older_hs) + 5:
                health_trend = "improving"
            elif sum(recent_hs) / len(recent_hs) < sum(older_hs) / len(older_hs) - 5:
                health_trend = "declining"

    return {
        "attendees_filter": list(target_names) if target_names else None,
        "meeting_count": len(series),
        "health_trend": health_trend,
        "series": series,
    }


@router.get("/analytics/api-usage", tags=["Analytics"])
async def get_api_usage(request: Request):
    """Account-level API usage summary for the current 24-hour window.

    Returns bot counts, token consumption, cost breakdown, error rate,
    and top platforms — useful for building a usage dashboard.
    """
    account_id = getattr(request.state, "account_id", None)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None

    cache_key = f"api_usage:{filter_account}"
    cached = _cache_get(_api_usage_cache, cache_key, _API_USAGE_TTL)
    if cached is not None:
        return cached

    all_bots = await _get_bots(filter_account)

    now = _now()
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    bots_7d = [b for b in all_bots if b.created_at and b.created_at >= seven_days_ago]
    bots_30d = [b for b in all_bots if b.created_at and b.created_at >= thirty_days_ago]

    tokens_7d = sum(b.ai_total_tokens for b in bots_7d)
    cost_7d = sum(b.ai_total_cost_usd for b in bots_7d)

    done_7d = sum(1 for b in bots_7d if b.status == "done")
    error_7d = sum(1 for b in bots_7d if b.status == "error")
    finished_7d = done_7d + error_7d
    error_rate_7d = round(error_7d / finished_7d, 3) if finished_7d > 0 else None

    platform_counts: dict[str, int] = {}
    for bot in bots_7d:
        platform_counts[bot.meeting_platform] = platform_counts.get(bot.meeting_platform, 0) + 1
    top_platforms = sorted(platform_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # AI operation breakdown for last 7 days
    op_tokens: dict[str, int] = {}
    op_cost: dict[str, float] = {}
    for bot in bots_7d:
        for usage in bot.ai_usage:
            op = usage.get("operation", "unknown")
            op_tokens[op] = op_tokens.get(op, 0) + usage.get("total_tokens", 0)
            op_cost[op] = op_cost.get(op, 0.0) + usage.get("cost_usd", 0.0)

    result = {
        "window": "24h_in_memory",
        "bots_created_7d": len(bots_7d),
        "bots_created_30d": len(bots_30d),
        "total_tokens_7d": tokens_7d,
        "total_cost_usd_7d": round(cost_7d, 6),
        "error_rate_7d": error_rate_7d,
        "top_platforms_7d": [{"platform": p, "count": c} for p, c in top_platforms],
        "tokens_by_operation_7d": op_tokens,
        "cost_by_operation_7d": {k: round(v, 6) for k, v in op_cost.items()},
    }
    _cache_set(_api_usage_cache, cache_key, result)
    return result


@router.get("/search", tags=["Search"])
async def search_transcripts(
    q: str = Query(..., min_length=1, description="Search query — matched case-insensitively against transcript text."),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of matching snippets to return."),
    include_archived: bool = Query(
        True,
        description=(
            "When true (default), also searches archived bot snapshots in the database "
            "beyond the 24-hour in-memory window. Set false to search only active/recent bots."
        ),
    ),
    platform: str = Query(None, description="Filter results by meeting platform (zoom, google_meet, microsoft_teams)."),
    semantic: bool = Query(
        False,
        description=(
            "When true, embed the query and rank meetings by cosine similarity "
            "against stored summary embeddings. Falls back to substring search if "
            "no embeddings are available."
        ),
    ),
    request: Request = None,
):
    """Cross-meeting full-text search across transcripts.

    Searches both the in-memory 24-hour window AND persisted bot snapshots in the
    database when `include_archived=true` (the default).

    With `semantic=false` (default): fast substring search across all transcripts.
    With `semantic=true`: embed the query and rank meetings by cosine similarity
    against stored `summary_embedding` vectors (falls back to substring if no embeddings).

    Returns up to `limit` transcript snippets whose text contains the query
    string (case-insensitive), each annotated with its source bot context.
    Results are ordered by bot creation time (newest first).
    """
    account_id = getattr(request.state, "account_id", None)
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None
    all_bots, _ = await store.list_bots(limit=500, account_id=filter_account)

    # ── Semantic (embedding) search ──────────────────────────────────────────
    if semantic:
        from app.services.intelligence_service import embed_text
        import math

        query_embedding = await embed_text(q)
        if query_embedding:
            def _cosine(a: list, b: list) -> float:
                dot = sum(x * y for x, y in zip(a, b))
                mag_a = math.sqrt(sum(x * x for x in a))
                mag_b = math.sqrt(sum(x * x for x in b))
                return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0

            scored = []
            for bot in all_bots:
                if platform and bot.meeting_platform != platform:
                    continue
                if getattr(bot, "summary_embedding", None):
                    score = _cosine(query_embedding, bot.summary_embedding)
                    if score >= 0.6:
                        scored.append((score, bot))

            scored.sort(key=lambda x: x[0], reverse=True)
            results = [
                {
                    "bot_id": bot.id,
                    "meeting_url": bot.meeting_url,
                    "meeting_platform": bot.meeting_platform,
                    "score": round(score, 3),
                    "summary": (bot.analysis or {}).get("summary", ""),
                    "created_at": bot.created_at.isoformat() if bot.created_at else None,
                }
                for score, bot in scored[:limit]
            ]
            return {"query": q, "semantic": True, "total": len(results), "results": results}
        # Fall back to substring search if embedding unavailable

    # ── Substring search ─────────────────────────────────────────────────────
    q_lower = q.lower()
    matches: list[dict] = []
    seen_bot_ids: set[str] = set()

    # ── Search in-memory bots ─────────────────────────────────────────────────
    for bot in all_bots:
        seen_bot_ids.add(bot.id)
        if platform and bot.meeting_platform != platform:
            continue
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
                    "source": "memory",
                })
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break

    # ── Search archived DB snapshots ──────────────────────────────────────────
    if include_archived and len(matches) < limit:
        remaining = limit - len(matches)
        try:
            from app.db import AsyncSessionLocal
            from app.models.account import BotSnapshot
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                query = select(BotSnapshot)
                if filter_account:
                    query = query.where(BotSnapshot.account_id == filter_account)
                query = query.order_by(BotSnapshot.created_at.desc()).limit(500)
                result = await db.execute(query)
                snapshots = result.scalars().all()

            for snap in snapshots:
                if snap.id in seen_bot_ids:
                    continue  # already searched from memory
                try:
                    data = json.loads(snap.data)
                except Exception:
                    continue

                snap_platform = data.get("meeting_platform", "unknown")
                if platform and snap_platform != platform:
                    continue

                transcript = data.get("transcript") or []
                for entry in transcript:
                    text = entry.get("text", "") or ""
                    if q_lower in text.lower():
                        matches.append({
                            "bot_id": snap.id,
                            "meeting_url": data.get("meeting_url", ""),
                            "meeting_platform": snap_platform,
                            "bot_status": data.get("status", "done"),
                            "speaker": entry.get("speaker"),
                            "text": text,
                            "timestamp": entry.get("timestamp"),
                            "source": "archive",
                        })
                        if len(matches) >= limit:
                            break
                if len(matches) >= limit:
                    break
        except Exception as exc:
            logger.warning("Archive search failed: %s", exc)

    return {
        "query": q,
        "semantic": False,
        "total": len(matches),
        "include_archived": include_archived,
        "results": matches,
    }


# ── Audit Log ─────────────────────────────────────────────────────────────────

class AuditLogEntryResponse(BaseModel):
    id: str
    account_id: Optional[str] = None
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    ip_address: Optional[str] = None
    details: Optional[str] = None
    created_at: datetime


@router.get("/audit-log", response_model=list[AuditLogEntryResponse], tags=["Audit"])
async def get_audit_log(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: Optional[str] = Query(None, description="Filter by action prefix, e.g. 'bot.created'"),
    account_id_filter: Optional[str] = Query(None, alias="account_id", description="Admin only: filter by account ID"),
):
    """List audit log entries for the current account.

    Non-admin users see only their own entries. Admins may pass `account_id`
    to query any account's log.
    """
    requester_id: Optional[str] = getattr(request.state, "account_id", None)
    is_admin = requester_id == SUPERADMIN_ACCOUNT_ID

    # Determine which account to query
    if is_admin and account_id_filter:
        target_account_id = account_id_filter
    elif requester_id and requester_id != SUPERADMIN_ACCOUNT_ID:
        target_account_id = requester_id
    else:
        target_account_id = None  # superadmin without filter: return all

    try:
        from app.db import AsyncSessionLocal
        from app.models.account import AuditLog
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            query = select(AuditLog)
            if target_account_id:
                query = query.where(AuditLog.account_id == target_account_id)
            if action:
                query = query.where(AuditLog.action.like(f"{action}%"))
            query = query.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
            result = await db.execute(query)
            rows = result.scalars().all()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return [
        AuditLogEntryResponse(
            id=r.id,
            account_id=r.account_id,
            action=r.action,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            ip_address=r.ip_address,
            details=r.details,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ── GET /api/v1/analytics/me — personal usage dashboard ─────────────────────

@router.get("/analytics/me", tags=["Analytics"])
async def get_my_analytics(request: Request):
    """Personal usage statistics scoped strictly to the authenticated account.

    Returns meetings this month, AI cost, open action items count,
    average meeting duration, 4-week sentiment trend, and cost by platform.
    """
    account_id = getattr(request.state, "account_id", None)
    if not account_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_starts = [now - timedelta(weeks=i+1) for i in range(4)]

    # Use account-scoped store (never superadmin expanded)
    all_bots, _ = await store.list_bots(limit=10000, account_id=account_id)

    meetings_this_month = 0
    ai_cost_this_month = 0.0
    durations = []
    cost_by_platform: dict[str, float] = {}
    sentiment_by_week: list[dict] = []

    for bot in all_bots:
        created = bot.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created and created >= month_start:
            meetings_this_month += 1
            ai_cost_this_month += bot.ai_total_cost_usd
        if bot.duration_seconds:
            durations.append(bot.duration_seconds)
        plat = bot.meeting_platform or "unknown"
        cost_by_platform[plat] = round(cost_by_platform.get(plat, 0.0) + bot.ai_total_cost_usd, 4)

    # 4-week sentiment trend — average sentiment score per week (positive=1, neutral=0, negative=-1)
    for i, week_start in enumerate(week_starts):
        week_end = week_start + timedelta(weeks=1)
        if week_start.tzinfo is None:
            week_start = week_start.replace(tzinfo=timezone.utc)
        if week_end.tzinfo is None:
            week_end = week_end.replace(tzinfo=timezone.utc)
        week_bots = [
            b for b in all_bots
            if b.created_at and (b.created_at.replace(tzinfo=timezone.utc) if b.created_at.tzinfo is None else b.created_at) >= week_start
            and (b.created_at.replace(tzinfo=timezone.utc) if b.created_at.tzinfo is None else b.created_at) < week_end
            and b.analysis
        ]
        sentiments = [b.analysis.get("sentiment", "neutral") for b in week_bots if b.analysis]
        score_map = {"positive": 1, "neutral": 0, "negative": -1}
        avg_score = sum(score_map.get(s, 0) for s in sentiments) / len(sentiments) if sentiments else 0
        sentiment_by_week.append({
            "week": week_start.strftime("%Y-%m-%d"),
            "score": round(avg_score, 2),
            "meetings": len(week_bots),
        })

    # Open action items count from DB
    open_action_items = 0
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import ActionItem
        from sqlalchemy import select, func
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(func.count()).where(
                    ActionItem.account_id == account_id,
                    ActionItem.status == "open",
                )
            )
            open_action_items = result.scalar_one() or 0
    except Exception:
        logger.warning("Action items count query failed — defaulting to 0", exc_info=True)

    avg_duration_min = round(sum(durations) / len(durations) / 60, 1) if durations else 0

    return {
        "meetings_this_month": meetings_this_month,
        "ai_cost_this_month": round(ai_cost_this_month, 4),
        "total_action_items_open": open_action_items,
        "avg_meeting_duration_min": avg_duration_min,
        "sentiment_trend": list(reversed(sentiment_by_week)),  # oldest first
        "cost_by_platform": cost_by_platform,
    }


# ── Usage breakdown ────────────────────────────────────────────────────────────


@router.get("/analytics/usage", tags=["Analytics"])
async def get_usage(request: Request):
    """Monthly usage breakdown for the authenticated account: bots used, limit, cost, daily chart."""
    account_id = getattr(request.state, "account_id", None)
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Requires per-user authentication")

    from app.db import AsyncSessionLocal
    from app.models.account import Account, CreditTransaction, BotSnapshot
    from sqlalchemy import select, func, Date, cast
    from app.config import settings

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")

        limit = settings.plan_limits.get(account.plan or "free", settings.PLAN_FREE_BOTS_PER_MONTH)
        bots_used = account.monthly_bots_used or 0

        # Credits spent this month (bot_usage transactions, negative amounts)
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        spent_result = await db.execute(
            select(func.coalesce(func.sum(CreditTransaction.amount_usd), 0))
            .where(
                CreditTransaction.account_id == account_id,
                CreditTransaction.type == "bot_usage",
                CreditTransaction.created_at >= month_start,
            )
        )
        credits_spent = abs(float(spent_result.scalar() or 0))

        # Daily usage from bot snapshots this month
        _date_expr = cast(BotSnapshot.created_at, Date)
        daily_result = await db.execute(
            select(_date_expr.label("day"), func.count().label("cnt"))
            .where(
                BotSnapshot.account_id == account_id,
                BotSnapshot.created_at >= month_start,
            )
            .group_by(_date_expr)
            .order_by(_date_expr)
        )
        daily_usage = [{"date": str(r.day), "count": r.cnt} for r in daily_result]

    avg_cost = round(credits_spent / bots_used, 4) if bots_used > 0 else 0.0

    return {
        "bots_used": bots_used,
        "bots_limit": limit,
        "plan": account.plan or "free",
        "credits_balance": float(account.credits_usd or 0),
        "credits_spent_this_month": round(credits_spent, 4),
        "avg_cost_per_bot": avg_cost,
        "billing_cycle_reset": account.monthly_reset_at.isoformat() if account.monthly_reset_at else None,
        "daily_usage": daily_usage,
    }


# ── Longitudinal Trends ───────────────────────────────────────────────────────


@router.get("/analytics/trends", tags=["Analytics"])
async def get_trends(
    request: Request,
    days: int = Query(30, ge=7, le=365, description="Number of days to look back"),
):
    """Longitudinal analytics: meetings/day, sentiment trend, topic frequency, cost trend."""
    account_id = getattr(request.state, "account_id", None)
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Requires per-user authentication")

    from app.db import AsyncSessionLocal
    from app.models.account import MeetingSummary
    from sqlalchemy import select, func, Date, cast

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MeetingSummary)
            .where(MeetingSummary.account_id == account_id, MeetingSummary.created_at >= cutoff)
            .order_by(MeetingSummary.created_at.desc())
            .limit(5000)
        )
        summaries = result.scalars().all()

    if not summaries:
        return {
            "range_days": days, "total_meetings": 0, "total_hours": 0,
            "meetings_per_day": [], "sentiment_trend": [], "health_trend": [],
            "top_topics": [], "cost_trend": [],
        }

    # Aggregate by day
    from collections import Counter, defaultdict
    daily_meetings: dict[str, int] = defaultdict(int)
    daily_sentiment: dict[str, list] = defaultdict(list)
    daily_health: dict[str, list] = defaultdict(list)
    daily_cost: dict[str, float] = defaultdict(float)
    topic_counter: Counter = Counter()
    total_seconds = 0

    for s in summaries:
        day = s.created_at.strftime("%Y-%m-%d") if s.created_at else "unknown"
        daily_meetings[day] += 1
        if s.sentiment is not None:
            daily_sentiment[day].append(float(s.sentiment))
        if s.health_score is not None:
            daily_health[day].append(s.health_score)
        if s.ai_cost_usd is not None:
            daily_cost[day] += float(s.ai_cost_usd)
        total_seconds += s.duration_seconds or 0
        if s.topics:
            try:
                for t in json.loads(s.topics):
                    if t:
                        topic_counter[t] += 1
            except Exception:
                pass

    # Build sorted time series
    all_days = sorted(set(daily_meetings.keys()))
    meetings_per_day = [{"date": d, "count": daily_meetings[d]} for d in all_days]
    sentiment_trend = [
        {"date": d, "avg": round(sum(daily_sentiment[d]) / len(daily_sentiment[d]), 2)}
        for d in all_days if daily_sentiment[d]
    ]
    health_trend = [
        {"date": d, "avg": round(sum(daily_health[d]) / len(daily_health[d]), 1)}
        for d in all_days if daily_health[d]
    ]
    cost_trend = [{"date": d, "cost_usd": round(daily_cost[d], 4)} for d in all_days if daily_cost[d]]
    top_topics = [{"topic": t, "count": c} for t, c in topic_counter.most_common(20)]

    return {
        "range_days": days,
        "total_meetings": len(summaries),
        "total_hours": round(total_seconds / 3600, 1),
        "meetings_per_day": meetings_per_day,
        "sentiment_trend": sentiment_trend,
        "health_trend": health_trend,
        "top_topics": top_topics,
        "cost_trend": cost_trend,
    }
