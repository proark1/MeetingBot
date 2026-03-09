"""Meeting analytics — trends, sentiment distribution, top topics."""

import logging
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.bot import Bot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("")
async def get_analytics(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Aggregate analytics across all completed meetings."""
    bots = (
        await db.execute(
            select(Bot).where(Bot.status == "done").order_by(Bot.created_at.desc())
        )
    ).scalars().all()

    if not bots:
        return _empty_analytics()

    # ── Meetings per day (last 30 days) ────────────────────────────────────
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    per_day: dict[str, int] = {}
    for b in bots:
        if b.created_at and b.created_at.replace(tzinfo=timezone.utc) >= cutoff:
            day = b.created_at.strftime("%Y-%m-%d")
            per_day[day] = per_day.get(day, 0) + 1

    # ── Avg duration ───────────────────────────────────────────────────────
    durations = []
    for b in bots:
        if b.started_at and b.ended_at:
            d = (b.ended_at - b.started_at).total_seconds()
            if 0 < d < 86400:
                durations.append(d)
    avg_duration_s = round(sum(durations) / len(durations)) if durations else 0

    # ── Sentiment distribution ─────────────────────────────────────────────
    sentiment_counts: dict[str, int] = {"positive": 0, "neutral": 0, "negative": 0}
    for b in bots:
        s = (b.analysis or {}).get("sentiment", "neutral")
        if s in sentiment_counts:
            sentiment_counts[s] += 1

    # ── Top topics ─────────────────────────────────────────────────────────
    topic_counter: Counter = Counter()
    for b in bots:
        for t in (b.analysis or {}).get("topics", []):
            topic_counter[t.lower().strip()] += 1
    top_topics = [{"topic": t, "count": c} for t, c in topic_counter.most_common(10)]

    # ── Most active participants ────────────────────────────────────────────
    participant_counter: Counter = Counter()
    for b in bots:
        for p in (b.participants or []):
            participant_counter[p.strip()] += 1
    top_participants = [
        {"name": p, "meetings": c}
        for p, c in participant_counter.most_common(10)
    ]

    # ── Platform breakdown ─────────────────────────────────────────────────
    platform_counter: Counter = Counter(b.meeting_platform for b in bots)
    platform_breakdown = dict(platform_counter.most_common())

    return {
        "total_meetings": len(bots),
        "avg_duration_s": avg_duration_s,
        "avg_duration_fmt": _fmt_duration(avg_duration_s),
        "sentiment_distribution": sentiment_counts,
        "meetings_per_day": [
            {"date": d, "count": c}
            for d, c in sorted(per_day.items())
        ],
        "top_topics": top_topics,
        "top_participants": top_participants,
        "platform_breakdown": platform_breakdown,
    }


def _fmt_duration(secs: int) -> str:
    if secs <= 0:
        return "—"
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def _empty_analytics() -> dict:
    return {
        "total_meetings": 0,
        "avg_duration_s": 0,
        "avg_duration_fmt": "—",
        "sentiment_distribution": {"positive": 0, "neutral": 0, "negative": 0},
        "meetings_per_day": [],
        "top_topics": [],
        "top_participants": [],
        "platform_breakdown": {},
    }
