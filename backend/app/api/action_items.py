"""Action item tracking endpoints."""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.db import AsyncSessionLocal
from app.deps import SUPERADMIN_ACCOUNT_ID
from app.models.account import ActionItem, ActionItemApproval
from sqlalchemy import select

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

    model_config = {
        "from_attributes": True,
        "json_schema_extra": {"example": {
            "id": "ai_8a72c5e1abcd1234ef567890abcdef12",
            "account_id": "550e8400-e29b-41d4-a716-446655440000",
            "bot_id": "bot_8a72c5e1",
            "task": "Wire up the v2 onboarding A/B test",
            "assignee": "Alice",
            "due_date": "2026-05-18",
            "confidence": 0.93,
            "status": "open",
            "created_at": "2026-05-04T15:34:18Z",
            "completed_at": None,
        }},
    }


class ActionItemPatch(BaseModel):
    status: Optional[str] = None  # "open" or "done"
    assignee: Optional[str] = None
    due_date: Optional[str] = None

    model_config = {"json_schema_extra": {"examples": [
        {"status": "done"},
        {"assignee": "Alice", "due_date": "2026-05-18"},
    ]}}


class ActionItemApprovalResponse(BaseModel):
    id: str
    account_id: Optional[str] = None
    bot_id: str
    action_item_id: Optional[str] = None
    integration_id: str
    integration_type: str
    destination_type: str
    payload: dict
    status: str
    error_message: Optional[str] = None
    reviewed_by: Optional[str] = None
    created_at: datetime
    reviewed_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None

    model_config = {"json_schema_extra": {"example": {
        "id": "apr_8a72c5e1",
        "account_id": "550e8400-e29b-41d4-a716-446655440000",
        "bot_id": "bot_8a72c5e1",
        "action_item_id": "ai_8a72c5e1abcd1234ef567890abcdef12",
        "integration_id": "int_4cb812aa",
        "integration_type": "linear",
        "destination_type": "task",
        "payload": {"task": "Send the proposal", "assignee": "Alex", "due_date": "2026-06-30"},
        "status": "pending",
        "error_message": None,
        "reviewed_by": None,
        "created_at": "2026-06-24T10:00:00Z",
        "reviewed_at": None,
        "delivered_at": None,
    }}}


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


def _approval_to_response(row: ActionItemApproval) -> ActionItemApprovalResponse:
    try:
        payload = json.loads(row.payload or "{}")
    except Exception:
        payload = {}
    return ActionItemApprovalResponse(
        id=row.id,
        account_id=row.account_id,
        bot_id=row.bot_id,
        action_item_id=row.action_item_id,
        integration_id=row.integration_id,
        integration_type=row.integration_type,
        destination_type=row.destination_type,
        payload=payload,
        status=row.status,
        error_message=row.error_message,
        reviewed_by=row.reviewed_by,
        created_at=row.created_at,
        reviewed_at=row.reviewed_at,
        delivered_at=row.delivered_at,
    )


@router.get(
    "",
    response_model=list[ActionItemResponse],
    responses={200: {"content": {"application/json": {"example": [{
        "id": "ai_8a72c5e1abcd1234ef567890abcdef12",
        "account_id": "550e8400-e29b-41d4-a716-446655440000",
        "bot_id": "bot_8a72c5e1",
        "task": "Wire up the v2 onboarding A/B test",
        "assignee": "Alice",
        "due_date": "2026-05-18",
        "confidence": 0.93,
        "status": "open",
        "created_at": "2026-05-04T15:34:18Z",
        "completed_at": None,
    }]}}}},
)
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


@router.get("/approvals", response_model=list[ActionItemApprovalResponse])
async def list_action_item_approvals(
    request: Request,
    status: Optional[str] = Query(default="pending", description="Filter by approval status"),
    integration_type: Optional[str] = Query(default=None, description="Filter by integration type"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List queued CRM/task approvals for the authenticated account."""
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")
    sub_user_id = (request.headers.get("X-Sub-User", "").strip()[:255]) or None

    async with AsyncSessionLocal() as session:
        q = select(ActionItemApproval).where(ActionItemApproval.account_id == account_id)
        if sub_user_id is not None:
            q = q.where(ActionItemApproval.sub_user_id == sub_user_id)
        if status:
            q = q.where(ActionItemApproval.status == status)
        if integration_type:
            q = q.where(ActionItemApproval.integration_type == integration_type)
        q = q.order_by(ActionItemApproval.created_at.desc()).limit(limit).offset(offset)
        result = await session.execute(q)
        rows = result.scalars().all()
    return [_approval_to_response(r) for r in rows]


@router.post("/approvals/{approval_id}/approve", response_model=ActionItemApprovalResponse)
async def approve_action_item(approval_id: str, request: Request):
    """Approve and dispatch a queued CRM/task action."""
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")
    sub_user_id = (request.headers.get("X-Sub-User", "").strip()[:255]) or None

    async with AsyncSessionLocal() as session:
        q = select(ActionItemApproval).where(
            ActionItemApproval.id == approval_id,
            ActionItemApproval.account_id == account_id,
        )
        if sub_user_id is not None:
            q = q.where(ActionItemApproval.sub_user_id == sub_user_id)
        result = await session.execute(q)
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        if row.status not in {"pending", "failed"}:
            raise HTTPException(status_code=409, detail=f"Approval is already {row.status}")

        from app.services.approval_service import approve_and_dispatch
        row = await approve_and_dispatch(row, session, account_id)
        await session.commit()
        await session.refresh(row)
    return _approval_to_response(row)


@router.post("/approvals/{approval_id}/reject", response_model=ActionItemApprovalResponse)
async def reject_action_item(approval_id: str, request: Request):
    """Reject a queued CRM/task action without dispatching it."""
    account_id: Optional[str] = getattr(request.state, "account_id", None)
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")
    sub_user_id = (request.headers.get("X-Sub-User", "").strip()[:255]) or None

    async with AsyncSessionLocal() as session:
        q = select(ActionItemApproval).where(
            ActionItemApproval.id == approval_id,
            ActionItemApproval.account_id == account_id,
        )
        if sub_user_id is not None:
            q = q.where(ActionItemApproval.sub_user_id == sub_user_id)
        result = await session.execute(q)
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found")
        if row.status not in {"pending", "failed"}:
            raise HTTPException(status_code=409, detail=f"Approval is already {row.status}")
        row.status = "rejected"
        row.reviewed_by = account_id
        row.reviewed_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(row)
    return _approval_to_response(row)


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
    # Build all rows first, then batch-check existing hashes in one query
    rows_to_insert = []
    hashes = []
    for item in items:
        task_text = (item.get("task") or "").strip()
        if not task_text:
            continue
        content_hash = hashlib.sha256(f"{bot_id}:{task_text.lower()}".encode()).hexdigest()
        hashes.append(content_hash)
        rows_to_insert.append((content_hash, task_text, item))

    if not rows_to_insert:
        return

    async with AsyncSessionLocal() as session:
        # Single query to find all existing hashes
        existing_result = await session.execute(
            select(ActionItem.content_hash).where(ActionItem.content_hash.in_(hashes))
        )
        existing_hashes = {row[0] for row in existing_result}

        for content_hash, task_text, item in rows_to_insert:
            if content_hash in existing_hashes:
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
