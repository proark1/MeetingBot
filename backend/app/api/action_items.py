"""Cross-meeting action item tracker."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.action_item import ActionItem
from app.models.bot import Bot

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/action-items", tags=["Action Items"])


@router.get("")
async def list_action_items(
    db: Annotated[AsyncSession, Depends(get_db)],
    done: bool | None = None,
    assignee: str | None = None,
    limit: int = 100,
):
    """List all action items across all meetings, optionally filtered."""
    q = select(ActionItem).order_by(ActionItem.created_at.desc())
    if done is not None:
        q = q.where(ActionItem.done == done)
    if assignee:
        q = q.where(ActionItem.assignee.ilike(f"%{assignee}%"))
    items = (await db.execute(q.limit(limit))).scalars().all()

    # Enrich with meeting metadata
    bot_ids = list({i.bot_id for i in items})
    bots = {}
    if bot_ids:
        bot_rows = (await db.execute(
            select(Bot.id, Bot.meeting_url, Bot.meeting_platform, Bot.bot_name, Bot.started_at)
            .where(Bot.id.in_(bot_ids))
        )).all()
        bots = {r.id: r for r in bot_rows}

    return [
        {
            "id": i.id,
            "bot_id": i.bot_id,
            "task": i.task,
            "assignee": i.assignee,
            "due_date": i.due_date,
            "done": i.done,
            "created_at": i.created_at.isoformat(),
            "meeting_url": bots[i.bot_id].meeting_url if i.bot_id in bots else None,
            "meeting_platform": bots[i.bot_id].meeting_platform if i.bot_id in bots else None,
            "bot_name": bots[i.bot_id].bot_name if i.bot_id in bots else None,
            "started_at": (
                bots[i.bot_id].started_at.isoformat()
                if i.bot_id in bots and bots[i.bot_id].started_at
                else None
            ),
        }
        for i in items
    ]


@router.patch("/{item_id}")
async def update_action_item(
    item_id: str,
    payload: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Toggle done state or update assignee/due_date."""
    result = await db.execute(select(ActionItem).where(ActionItem.id == item_id))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Action item not found")
    if "done" in payload:
        item.done = bool(payload["done"])
    if "assignee" in payload:
        item.assignee = payload["assignee"]
    if "due_date" in payload:
        item.due_date = payload["due_date"]
    await db.commit()
    return {"id": item.id, "done": item.done, "assignee": item.assignee, "due_date": item.due_date}


@router.get("/stats")
async def action_item_stats(db: Annotated[AsyncSession, Depends(get_db)]):
    """Count total / done / pending action items."""
    all_items = (await db.execute(select(ActionItem))).scalars().all()
    total = len(all_items)
    done = sum(1 for i in all_items if i.done)
    return {"total": total, "done": done, "pending": total - done}
