"""Action item tracking endpoints."""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.db import AsyncSessionLocal
from app.deps import SUPERADMIN_ACCOUNT_ID
from app.models.account import ActionItem
from sqlalchemy import select, update

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/action-items", tags=["Action Items"])


class ActionItemResponse(BaseModel):
    id: str
    account_id: Optional[str] = None
    bot_id: str
    task: str
    assignee: Optional[str] = None
    due_date: Optional[str] = None
    confidence: Optional[float] = None
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ActionItemPatch(BaseModel):
    status: Optional[str] = None  # "open" or "done"
    assignee: Optional[str] = None
    due_date: Optional[str] = None


def _to_response(row: ActionItem) -> ActionItemResponse:
    return ActionItemResponse(
        id=row.id,
        account_id=row.account_id,
        bot_id=row.bot_id,
        task=row.task,
        assignee=row.assignee,
        due_date=row.due_date,
        confidence=float(row.confidence) if row.confidence is not None else None,
        status=row.status,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


@router.get("", response_model=list[ActionItemResponse])
async def list_action_items(
    request: Request,
    status: Optional[str] = Query(default=None, description="Filter by status: open or done"),
    assignee: Optional[str] = Query(default=None, description="Case-insensitive substring match on assignee"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List action items for the authenticated account."""
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = (request.headers.get("X-Sub-User", "").strip()[:255]) or None

    async with AsyncSessionLocal() as session:
        q = select(ActionItem)
        if account_id and account_id != SUPERADMIN_ACCOUNT_ID:
            q = q.where(ActionItem.account_id == account_id)
        if sub_user_id is not None:
            q = q.where(ActionItem.sub_user_id == sub_user_id)
        if status:
            q = q.where(ActionItem.status == status)
        if assignee:
            q = q.where(ActionItem.assignee.ilike(f"%{assignee}%"))
        q = q.order_by(ActionItem.created_at.desc()).limit(limit).offset(offset)
        result = await session.execute(q)
        rows = result.scalars().all()

    return [_to_response(r) for r in rows]


@router.patch("/{item_id}", response_model=ActionItemResponse)
async def patch_action_item(item_id: str, request: Request, payload: ActionItemPatch):
    """Update an action item's status, assignee, or due date."""
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    sub_user_id = (request.headers.get("X-Sub-User", "").strip()[:255]) or None

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ActionItem).where(ActionItem.id == item_id))
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Action item {item_id!r} not found")
        if account_id and account_id != SUPERADMIN_ACCOUNT_ID and row.account_id != account_id:
            raise HTTPException(status_code=404, detail=f"Action item {item_id!r} not found")
        if sub_user_id is not None and getattr(row, "sub_user_id", None) != sub_user_id:
            raise HTTPException(status_code=404, detail=f"Action item {item_id!r} not found")

        if payload.status is not None:
            if payload.status not in ("open", "done"):
                raise HTTPException(status_code=400, detail="status must be 'open' or 'done'")
            row.status = payload.status
            if payload.status == "done" and row.completed_at is None:
                row.completed_at = datetime.now(timezone.utc)
            elif payload.status == "open":
                row.completed_at = None
        if payload.assignee is not None:
            row.assignee = payload.assignee
        if payload.due_date is not None:
            row.due_date = payload.due_date

        await session.commit()
        await session.refresh(row)

    return _to_response(row)


async def upsert_action_items(account_id: Optional[str], bot_id: str, items: list[dict], sub_user_id: Optional[str] = None) -> None:
    """Called after analysis completes to persist action items to the DB.

    Uses a content hash (sha256 of bot_id + task text) for idempotent upsert.
    """
    if not items:
        return
    async with AsyncSessionLocal() as session:
        for item in items:
            task_text = (item.get("task") or "").strip()
            if not task_text:
                continue
            content_hash = hashlib.sha256(f"{bot_id}:{task_text.lower()}".encode()).hexdigest()
            # Check if already exists
            existing = await session.execute(
                select(ActionItem).where(ActionItem.content_hash == content_hash)
            )
            if existing.scalar_one_or_none() is not None:
                continue
            row = ActionItem(
                id=hashlib.sha256(f"{bot_id}:{task_text}".encode()).hexdigest()[:36],
                account_id=account_id,
                sub_user_id=sub_user_id,
                bot_id=bot_id,
                content_hash=content_hash,
                task=task_text,
                assignee=item.get("assignee"),
                due_date=item.get("due_date"),
                confidence=float(item["confidence"]) if item.get("confidence") is not None else None,
                status="open",
            )
            session.add(row)
        await session.commit()
