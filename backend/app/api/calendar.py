"""Calendar API — manage iCal feeds for auto-join and trigger manual syncs."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.webhooks import _block_ssrf
from app.db import get_db
from app.deps import get_current_account_id, SUPERADMIN_ACCOUNT_ID
from app.models.account import CalendarFeed

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/calendar", tags=["Calendar"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class CalendarFeedCreate(BaseModel):
    name: str = Field(default="My Calendar", max_length=100)
    ical_url: str = Field(
        description=(
            "Publicly accessible iCal (.ics) URL, e.g. a Google Calendar private address "
            "or Outlook subscription link."
        )
    )
    bot_name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Custom bot display name for meetings from this calendar. Defaults to account bot name.",
    )
    auto_record: bool = Field(default=True, description="Automatically join and record detected meetings.")
    is_active: bool = Field(default=True)

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Work Calendar",
                "ical_url": "https://calendar.google.com/calendar/ical/you%40gmail.com/private-abc/basic.ics",
                "bot_name": "Recorder",
                "auto_record": True,
            }
        }
    }


class CalendarFeedResponse(BaseModel):
    id: str
    name: str
    ical_url: str
    bot_name: Optional[str]
    auto_record: bool
    is_active: bool
    last_synced_at: Optional[str]
    created_at: str


def _to_response(feed: CalendarFeed) -> CalendarFeedResponse:
    return CalendarFeedResponse(
        id=feed.id,
        name=feed.name,
        ical_url=feed.ical_url,
        bot_name=feed.bot_name,
        auto_record=feed.auto_record,
        is_active=feed.is_active,
        last_synced_at=feed.last_synced_at.isoformat() if feed.last_synced_at else None,
        created_at=feed.created_at.isoformat(),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=list[CalendarFeedResponse])
async def list_feeds(
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """List all calendar feeds for the current account."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")

    result = await db.execute(
        select(CalendarFeed)
        .where(CalendarFeed.account_id == account_id)
        .order_by(CalendarFeed.created_at.desc())
    )
    return [_to_response(f) for f in result.scalars().all()]


@router.post("", response_model=CalendarFeedResponse, status_code=201)
async def create_feed(
    payload: CalendarFeedCreate,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Add a new iCal feed for calendar auto-join."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")

    from app.deps import check_feature
    await check_feature("calendar_auto_join", account_id, db)

    await _block_ssrf(payload.ical_url)

    feed = CalendarFeed(
        id=str(uuid.uuid4()),
        account_id=account_id,
        name=payload.name,
        ical_url=payload.ical_url,
        bot_name=payload.bot_name,
        auto_record=payload.auto_record,
        is_active=payload.is_active,
    )
    db.add(feed)
    await db.commit()
    await db.refresh(feed)

    logger.info("Account %s added calendar feed %s (%s)", account_id, feed.id, payload.name)
    return _to_response(feed)


@router.patch("/{feed_id}", response_model=CalendarFeedResponse)
async def update_feed(
    feed_id: str,
    payload: CalendarFeedCreate,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Update a calendar feed."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")

    result = await db.execute(
        select(CalendarFeed).where(
            CalendarFeed.id == feed_id,
            CalendarFeed.account_id == account_id,
        )
    )
    feed = result.scalar_one_or_none()
    if not feed:
        raise HTTPException(status_code=404, detail="Calendar feed not found")

    await _block_ssrf(payload.ical_url)
    feed.name = payload.name
    feed.ical_url = payload.ical_url
    feed.bot_name = payload.bot_name
    feed.auto_record = payload.auto_record
    feed.is_active = payload.is_active
    await db.commit()
    await db.refresh(feed)
    return _to_response(feed)


@router.delete("/{feed_id}", status_code=204)
async def delete_feed(
    feed_id: str,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Delete a calendar feed."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")

    result = await db.execute(
        select(CalendarFeed).where(
            CalendarFeed.id == feed_id,
            CalendarFeed.account_id == account_id,
        )
    )
    feed = result.scalar_one_or_none()
    if not feed:
        raise HTTPException(status_code=404, detail="Calendar feed not found")

    await db.delete(feed)
    await db.commit()


@router.post("/{feed_id}/sync", status_code=200)
async def sync_feed(
    feed_id: str,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger an immediate sync of a calendar feed.

    The background poll loop syncs every `CALENDAR_POLL_INTERVAL_S` seconds
    automatically; use this endpoint to force an immediate check.
    """
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Use per-user authentication")

    result = await db.execute(
        select(CalendarFeed).where(
            CalendarFeed.id == feed_id,
            CalendarFeed.account_id == account_id,
        )
    )
    feed = result.scalar_one_or_none()
    if not feed:
        raise HTTPException(status_code=404, detail="Calendar feed not found")

    from app.services.calendar_service import _process_feed
    dispatched = await _process_feed(feed, account_id)

    if dispatched:
        feed.last_synced_at = datetime.now(timezone.utc)
        await db.commit()

    return {"dispatched": dispatched, "message": f"Dispatched {dispatched} bot(s) from this feed"}
