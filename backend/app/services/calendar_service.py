"""Calendar auto-join service.

Polls iCal feeds for upcoming meetings and automatically dispatches bots.
Runs as a background task every CALENDAR_POLL_INTERVAL_S seconds.

Requires: icalendar, recurring_ical_events packages
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# How far ahead to look for meetings (minutes)
_LOOKAHEAD_MINUTES = 15
# How early to join before the scheduled start (seconds)
_JOIN_EARLY_S = 60
# Avoid dispatching the same event twice within this window (seconds)
_DISPATCH_COOLDOWN_S = 3600

# In-memory set of (feed_id, event_uid) already dispatched this session
_dispatched: set[tuple[str, str]] = set()


def _parse_ical(ical_data: bytes, lookahead_minutes: int = _LOOKAHEAD_MINUTES) -> list[dict[str, Any]]:
    """Parse iCal data and return upcoming events within the lookahead window."""
    try:
        import icalendar
        import recurring_ical_events
    except ImportError:
        raise RuntimeError(
            "icalendar and recurring_ical_events are required — "
            "run: pip install icalendar recurring-ical-events"
        )

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=lookahead_minutes)

    cal = icalendar.Calendar.from_ical(ical_data)
    events = recurring_ical_events.of(cal).between(now, window_end)

    results = []
    for event in events:
        uid = str(event.get("UID", ""))
        summary = str(event.get("SUMMARY", "Meeting"))
        description = str(event.get("DESCRIPTION", ""))
        location = str(event.get("LOCATION", ""))

        # Try to find a video conferencing URL
        meeting_url = None
        # 1. Check CONFERENCE / URL fields
        for field in ("URL", "CONFERENCE"):
            val = event.get(field)
            if val:
                url_str = str(val)
                if _is_video_url(url_str):
                    meeting_url = url_str
                    break
        # 2. Scan description for known video URLs
        if not meeting_url:
            meeting_url = _extract_video_url(description)
        # 3. Scan location
        if not meeting_url:
            meeting_url = _extract_video_url(location)

        if not meeting_url:
            continue  # No video link — skip

        dtstart = event.get("DTSTART")
        if dtstart is None:
            continue
        start = dtstart.dt
        if not hasattr(start, "tzinfo"):
            start = datetime.combine(start, datetime.min.time()).replace(tzinfo=timezone.utc)
        elif start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)

        results.append({
            "uid": uid,
            "summary": summary,
            "start": start,
            "meeting_url": meeting_url,
        })

    return results


_VIDEO_PATTERNS = [
    "meet.google.com",
    "zoom.us",
    "zoom.com",
    "teams.microsoft.com",
    "teams.live.com",
    "webex.com",
    "whereby.com",
]


def _is_video_url(url: str) -> bool:
    url_lower = url.lower()
    return any(p in url_lower for p in _VIDEO_PATTERNS)


def _extract_video_url(text: str) -> Optional[str]:
    """Extract first video conferencing URL found in text."""
    import re
    url_pattern = re.compile(
        r'https?://[^\s<>"\']+',
        re.IGNORECASE,
    )
    for m in url_pattern.finditer(text or ""):
        if _is_video_url(m.group(0)):
            return m.group(0)
    return None


async def _fetch_ical(url: str) -> bytes:
    """Fetch an iCal feed with a short timeout."""
    import httpx
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "MeetingBot/1.0 (iCal)"})
        resp.raise_for_status()
        return resp.content


async def _process_feed(feed, account_id: str) -> int:
    """Process a single calendar feed, dispatching bots for upcoming events.

    Returns the number of bots dispatched.
    """
    dispatched = 0
    try:
        ical_data = await _fetch_ical(feed.ical_url)
    except Exception as exc:
        logger.warning("Calendar feed %s fetch failed: %s", feed.id, exc)
        return 0

    try:
        events = _parse_ical(ical_data)
    except Exception as exc:
        logger.warning("Calendar feed %s parse failed: %s", feed.id, exc)
        return 0

    now = datetime.now(timezone.utc)

    for event in events:
        uid = event["uid"]
        cache_key = (feed.id, uid)
        if cache_key in _dispatched:
            continue

        # Schedule the bot to join early
        join_at = event["start"] - timedelta(seconds=_JOIN_EARLY_S)
        if join_at < now:
            join_at = now  # already past — join immediately

        try:
            from app.store import store, BotSession
            from app.api.bots import _schedule_bot

            bot = BotSession(
                meeting_url=event["meeting_url"],
                bot_name=feed.bot_name or "MeetingBot",
                join_at=join_at,
                account_id=account_id,
                metadata={"calendar_event": event["summary"], "calendar_feed_id": feed.id},
            )
            await _schedule_bot(bot)
            _dispatched.add(cache_key)
            dispatched += 1
            logger.info(
                "Calendar auto-join: dispatched bot for '%s' at %s (feed %s)",
                event["summary"], join_at.isoformat(), feed.id,
            )
        except Exception as exc:
            logger.error("Calendar auto-join dispatch failed for event %s: %s", uid, exc)

    return dispatched


async def sync_all_feeds() -> int:
    """Sync all active calendar feeds for all accounts.

    Returns total number of bots dispatched.
    """
    from app.db import AsyncSessionLocal
    from app.models.account import CalendarFeed
    from sqlalchemy import select

    total = 0
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(CalendarFeed).where(CalendarFeed.is_active == True)
            )
            feeds = result.scalars().all()
    except Exception as exc:
        logger.error("calendar_service: failed to load feeds: %s", exc)
        return 0

    for feed in feeds:
        count = await _process_feed(feed, feed.account_id)
        total += count
        if count:
            # Update last_synced_at
            try:
                async with AsyncSessionLocal() as session:
                    f = await session.get(type(feed), feed.id)
                    if f:
                        f.last_synced_at = datetime.now(timezone.utc)
                        await session.commit()
            except Exception:
                pass

    return total


async def calendar_poll_loop(interval_s: int = 300) -> None:
    """Background task: poll calendar feeds every interval_s seconds."""
    logger.info("Calendar poll loop started (interval=%ds)", interval_s)
    while True:
        try:
            dispatched = await sync_all_feeds()
            if dispatched:
                logger.info("Calendar sync: dispatched %d bot(s)", dispatched)
        except Exception as exc:
            logger.error("Calendar poll loop error: %s", exc)
        await asyncio.sleep(interval_s)
