"""Keyword alert CRUD API.

Keyword alerts fire a `bot.keyword_alert` webhook event whenever a configured
keyword or phrase is detected in a meeting transcript (live or post-processing).

Account-level alerts apply to all bots created by the account.
Per-bot alerts can also be specified at bot creation time.
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.deps import SUPERADMIN_ACCOUNT_ID

router = APIRouter(prefix="/keyword-alerts", tags=["Keyword Alerts"])


def _account_id(request: Request) -> str:
    """Extract required account_id from request state."""
    from fastapi import HTTPException
    account_id = getattr(request.state, "account_id", None)
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=401, detail="Authentication required")
    return account_id


class KeywordAlertCreate(BaseModel):
    name: str = Field(default="", max_length=100, description="Human-readable name for this alert.")
    keywords: list[str] = Field(description="List of keywords/phrases to watch for (case-insensitive).")
    webhook_url: Optional[str] = Field(
        default=None,
        description="Optional additional webhook URL to POST the alert to.",
    )
    is_active: bool = Field(default=True, description="Whether this alert is active.")


class KeywordAlertResponse(BaseModel):
    id: str
    account_id: str
    name: str
    keywords: list[str]
    webhook_url: Optional[str] = None
    is_active: bool
    trigger_count: int
    last_triggered_at: Optional[str] = None
    created_at: str


def _to_response(row) -> dict:
    try:
        keywords = json.loads(row.keywords or "[]")
    except Exception:
        keywords = []
    return {
        "id": row.id,
        "account_id": row.account_id,
        "name": row.name,
        "keywords": keywords,
        "webhook_url": row.webhook_url,
        "is_active": row.is_active,
        "trigger_count": row.trigger_count,
        "last_triggered_at": row.last_triggered_at.isoformat() if row.last_triggered_at else None,
        "created_at": row.created_at.isoformat(),
    }


@router.get("", response_model=list[KeywordAlertResponse])
async def list_keyword_alerts(request: Request):
    """List all keyword alerts for your account."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal
    from app.models.account import KeywordAlert
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(KeywordAlert)
            .where(KeywordAlert.account_id == account_id)
            .order_by(KeywordAlert.created_at.desc())
        )
        rows = result.scalars().all()

    return [_to_response(r) for r in rows]


@router.post("", response_model=KeywordAlertResponse, status_code=201)
async def create_keyword_alert(payload: KeywordAlertCreate, request: Request):
    """Create a new keyword alert.

    The alert will fire for all future bots in your account that contain
    any of the specified keywords in their transcript.
    """
    account_id = _account_id(request)

    if not payload.keywords:
        raise HTTPException(status_code=422, detail="At least one keyword is required")

    # Normalise keywords
    keywords = [k.strip() for k in payload.keywords if k.strip()]
    if not keywords:
        raise HTTPException(status_code=422, detail="Keywords must not be empty strings")

    from app.db import AsyncSessionLocal
    from app.models.account import KeywordAlert

    async with AsyncSessionLocal() as db:
        row = KeywordAlert(
            account_id=account_id,
            name=payload.name,
            keywords=json.dumps(keywords),
            webhook_url=payload.webhook_url,
            is_active=payload.is_active,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

    return _to_response(row)


@router.get("/{alert_id}", response_model=KeywordAlertResponse)
async def get_keyword_alert(alert_id: str, request: Request):
    """Get a specific keyword alert by ID."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal
    from app.models.account import KeywordAlert
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(KeywordAlert).where(
                KeywordAlert.id == alert_id,
                KeywordAlert.account_id == account_id,
            )
        )
        row = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Keyword alert {alert_id!r} not found")

    return _to_response(row)


@router.patch("/{alert_id}", response_model=KeywordAlertResponse)
async def update_keyword_alert(alert_id: str, payload: KeywordAlertCreate, request: Request):
    """Update an existing keyword alert."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal
    from app.models.account import KeywordAlert
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(KeywordAlert).where(
                KeywordAlert.id == alert_id,
                KeywordAlert.account_id == account_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Keyword alert {alert_id!r} not found")

        keywords = [k.strip() for k in payload.keywords if k.strip()]
        if not keywords:
            raise HTTPException(status_code=422, detail="Keywords must not be empty strings")

        row.name = payload.name
        row.keywords = json.dumps(keywords)
        row.webhook_url = payload.webhook_url
        row.is_active = payload.is_active
        await db.commit()
        await db.refresh(row)

    return _to_response(row)


@router.delete("/{alert_id}", status_code=204)
async def delete_keyword_alert(alert_id: str, request: Request):
    """Delete a keyword alert."""
    account_id = _account_id(request)

    from app.db import AsyncSessionLocal
    from app.models.account import KeywordAlert
    from sqlalchemy import select, delete

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(KeywordAlert).where(
                KeywordAlert.id == alert_id,
                KeywordAlert.account_id == account_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Keyword alert {alert_id!r} not found")
        await db.execute(delete(KeywordAlert).where(
            KeywordAlert.id == alert_id,
            KeywordAlert.account_id == account_id,
        ))
        await db.commit()
