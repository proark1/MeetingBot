"""Cross-meeting full-text transcript search."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.bot import Bot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/search", tags=["Search"])


@router.get("")
async def search_transcripts(
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = Query(..., min_length=2, description="Keyword to search across all transcripts"),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Search for a keyword across all meeting transcripts.

    Returns matching bots with the relevant transcript snippets highlighted.
    Uses SQLite json_each() for in-database JSON scanning.
    """
    if not q or len(q.strip()) < 2:
        raise HTTPException(status_code=422, detail="Query must be at least 2 characters")

    q = q.strip()
    pattern = f"%{q}%"

    # Use SQLite json_each to scan transcript entries
    raw_sql = text("""
        SELECT DISTINCT b.id, b.meeting_url, b.meeting_platform, b.bot_name,
               b.status, b.started_at, b.ended_at, b.participants, b.transcript
        FROM bots b, json_each(b.transcript) e
        WHERE (
            json_extract(e.value, '$.text') LIKE :pattern
            OR json_extract(e.value, '$.speaker') LIKE :pattern
        )
        AND b.status = 'done'
        ORDER BY b.created_at DESC
        LIMIT :limit
    """)

    try:
        rows = (await db.execute(raw_sql, {"pattern": pattern, "limit": limit})).mappings().all()
    except Exception as exc:
        logger.error("Search query failed: %s", exc)
        # Fallback: Python-side filtering for DBs where json_each may not work
        all_bots = (
            await db.execute(select(Bot).where(Bot.status == "done").order_by(Bot.created_at.desc()).limit(200))
        ).scalars().all()
        rows_fallback = []
        for b in all_bots:
            if any(
                q.lower() in (e.get("text", "") + e.get("speaker", "")).lower()
                for e in (b.transcript or [])
            ):
                rows_fallback.append(b)
            if len(rows_fallback) >= limit:
                break
        return _format_results(rows_fallback, q)

    return _format_results(rows, q)


def _format_results(rows, q: str) -> dict:
    results = []
    q_lower = q.lower()

    for row in rows:
        # Normalize: row can be a SQLAlchemy model or a RowMapping
        if hasattr(row, "transcript"):
            transcript = row.transcript or []
            bot_id = row.id
            bot_url = row.meeting_url
            bot_platform = row.meeting_platform
            bot_name = row.bot_name
            started_at = row.started_at.isoformat() if row.started_at else None
        else:
            import json as _json
            transcript = row["transcript"] if isinstance(row["transcript"], list) else _json.loads(row["transcript"] or "[]")
            bot_id = row["id"]
            bot_url = row["meeting_url"]
            bot_platform = row["meeting_platform"]
            bot_name = row["bot_name"]
            started_at = row["started_at"]

        # Find matching entries
        matches = [
            {
                "speaker": e.get("speaker", ""),
                "timestamp": e.get("timestamp", 0),
                "text": e.get("text", ""),
            }
            for e in transcript
            if q_lower in (e.get("text", "") + e.get("speaker", "")).lower()
        ][:5]  # max 5 snippets per meeting

        if matches:
            results.append({
                "bot_id": bot_id,
                "meeting_url": bot_url,
                "meeting_platform": bot_platform,
                "bot_name": bot_name,
                "started_at": started_at,
                "match_count": len(matches),
                "snippets": matches,
            })

    return {"query": q, "total": len(results), "results": results}
