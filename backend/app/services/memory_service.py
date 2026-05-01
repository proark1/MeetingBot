"""Cross-meeting memory retrieval.

Surfaces semantically related past meetings as context for a bot. The
memory pool is constrained by:
  - account scope (only the same account)
  - sub-user scope (when set)
  - workspace scope (optional)
  - lookback window (configurable per bot)

When ``cross_meeting_memory.inject_into_analysis`` is true, the related
summaries are passed as additional context to the post-meeting analysis
prompt via the ``previous_summaries`` parameter on
``intelligence_service.analyze_transcript``.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.deps import SUPERADMIN_ACCOUNT_ID
from app.services import intelligence_service
from app.store import store

logger = logging.getLogger(__name__)


def _cosine(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


async def retrieve_related(
    bot,
    query_text: Optional[str] = None,
    *,
    lookback_days: int = 30,
    max_meetings: int = 5,
    workspace_scope: str = "account",
    min_score: float = 0.55,
) -> list[dict]:
    """Find past meetings most similar to the bot's current context.

    ``query_text`` defaults to the bot's running transcript or the bot's
    summary when transcript is empty. Returns a list of records ordered
    by descending similarity score.
    """
    if not bot:
        return []

    # Choose a query string. Prefer current transcript head, fallback to
    # bot.metadata.title/agenda when transcript hasn't started.
    if not query_text:
        transcript = list(getattr(bot, "transcript", []) or [])[:30]
        if transcript:
            query_text = " ".join(e.get("text", "") for e in transcript)
        else:
            md = getattr(bot, "metadata", {}) or {}
            query_text = md.get("title") or md.get("agenda") or bot.meeting_url
    query_text = (query_text or "")[:4000]

    if not query_text.strip():
        return []

    try:
        query_embedding = await intelligence_service.embed_text(query_text)
    except Exception as exc:
        logger.debug("Memory retrieval: embed_text failed: %s", exc)
        query_embedding = None

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    account_id = bot.account_id
    filter_account = account_id if (account_id and account_id != SUPERADMIN_ACCOUNT_ID) else None

    candidates, _ = await store.list_bots(limit=10000, account_id=filter_account)

    # Apply scope filter
    bot_workspace = getattr(bot, "workspace_id", None)
    bot_sub_user = getattr(bot, "sub_user_id", None)
    filtered: list = []
    for past in candidates:
        if past.id == bot.id:
            continue
        if past.status not in ("done",):
            continue
        if not past.analysis:
            continue
        ca = past.created_at
        if ca and (ca if ca.tzinfo else ca.replace(tzinfo=timezone.utc)) < cutoff:
            continue
        if workspace_scope == "workspace" and bot_workspace:
            if getattr(past, "workspace_id", None) != bot_workspace:
                continue
        if workspace_scope == "sub_user" and bot_sub_user:
            if getattr(past, "sub_user_id", None) != bot_sub_user:
                continue
        filtered.append(past)

    results: list[dict] = []
    if query_embedding:
        scored: list[tuple[float, object]] = []
        for past in filtered:
            emb = getattr(past, "summary_embedding", None)
            if not emb:
                continue
            score = _cosine(query_embedding, emb)
            if score >= min_score:
                scored.append((score, past))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        for score, past in scored[:max_meetings]:
            summary = (past.analysis or {}).get("summary", "") if past.analysis else ""
            results.append({
                "bot_id": past.id,
                "meeting_url": past.meeting_url,
                "platform": past.meeting_platform,
                "score": round(score, 3),
                "summary": summary,
                "created_at": past.created_at.isoformat() if past.created_at else None,
            })
        if results:
            return results

    # Fallback: most recent N meetings (no embeddings available)
    filtered.sort(key=lambda b: b.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for past in filtered[:max_meetings]:
        summary = (past.analysis or {}).get("summary", "") if past.analysis else ""
        results.append({
            "bot_id": past.id,
            "meeting_url": past.meeting_url,
            "platform": past.meeting_platform,
            "score": None,
            "summary": summary,
            "created_at": past.created_at.isoformat() if past.created_at else None,
        })
    return results
