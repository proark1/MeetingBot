"""Calendar auto-join service.

Polls iCal feeds for upcoming meetings and automatically dispatches bots.
Runs as a background task every CALENDAR_POLL_INTERVAL_S seconds.

Requires: icalendar, recurring_ical_events packages
"""

import asyncio
import json
import logging
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# How far ahead to look for meetings (minutes)
_LOOKAHEAD_MINUTES = 15
# How early to join before the scheduled start (seconds)
_JOIN_EARLY_S = 60
# Avoid dispatching the same event twice within this window (seconds)
_DISPATCH_COOLDOWN_S = 3600
# Evict entries older than 48 hours to prevent unbounded growth
_DISPATCH_TTL_S = 172800

# Maps (feed_id, event_uid) → monotonic insertion time
_dispatched: dict[tuple[str, str], float] = {}
_poll_cycle_count = 0


def _prune_dispatched() -> None:
    cutoff = _time.monotonic() - _DISPATCH_TTL_S
    stale = [k for k, t in _dispatched.items() if t < cutoff]
    for k in stale:
        del _dispatched[k]


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

    try:
        cal = icalendar.Calendar.from_ical(ical_data)
    except Exception as exc:
        logger.warning("Failed to parse iCal data: %s", exc)
        return []
    try:
        events = recurring_ical_events.of(cal).between(now, window_end)
    except Exception as exc:
        logger.warning("Failed to query recurring events: %s", exc)
        return []

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


import httpx as _httpx
# follow_redirects must stay False so we can re-validate every redirect target
# against the SSRF guard (a 30x to 169.254.169.254 / 127.0.0.1 would otherwise
# leak cloud-metadata or internal HTTP into the iCal parser).
_http_client = _httpx.AsyncClient(timeout=15, follow_redirects=False)

_MAX_REDIRECTS = 5


async def _fetch_ical(url: str) -> bytes:
    """Fetch an iCal feed with SSRF re-validation and bounded redirect handling.

    Every fetch — including each redirect hop — is run through
    ``webhook_service.check_url_ssrf`` so a registered URL can't trick us into
    hitting RFC1918 / loopback / cloud-metadata addresses via DNS rebinding or
    a 30x to an internal target.
    """
    from app.services.webhook_service import check_url_ssrf

    headers = {"User-Agent": "JustHereToListen.io/1.0 (iCal)"}
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        ssrf_err = await check_url_ssrf(current)
        if ssrf_err is not None:
            raise RuntimeError(f"iCal SSRF blocked: {ssrf_err}")
        resp = await _http_client.get(current, headers=headers)
        if resp.status_code in (301, 302, 303, 307, 308):
            target = resp.headers.get("Location")
            if not target:
                resp.raise_for_status()
                return resp.content
            from urllib.parse import urljoin
            current = urljoin(current, target)
            continue
        resp.raise_for_status()
        return resp.content
    raise RuntimeError(f"iCal fetch exceeded {_MAX_REDIRECTS} redirects")


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
            import uuid as _uuid
            from app.store import store, BotSession
            from app.services import bot_service as _bot_service
            from app.api.bots import _bot_queue, _queue_event

            meeting_url = event["meeting_url"]
            bot = BotSession(
                id=str(_uuid.uuid4()),
                meeting_url=meeting_url,
                meeting_platform=_bot_service.detect_platform(meeting_url),
                bot_name=feed.bot_name or "JustHereToListen.io",
                status="scheduled",
                join_at=join_at,
                account_id=account_id,
                metadata={"calendar_event": event["summary"], "calendar_feed_id": feed.id},
            )
            await store.create_bot(bot)
            _bot_queue.append(bot.id)
            _queue_event.set()
            _dispatched[cache_key] = _time.monotonic()
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
    global _poll_cycle_count
    logger.info("Calendar poll loop started (interval=%ds)", interval_s)
    while True:
        try:
            dispatched = await sync_all_feeds()
            if dispatched:
                logger.info("Calendar sync: dispatched %d bot(s)", dispatched)
        except Exception as exc:
            logger.error("Calendar poll loop error: %s", exc)
        _poll_cycle_count += 1
        if _poll_cycle_count % 48 == 0:  # ~4h at 5-min intervals (was 288/24h)
            _prune_dispatched()
        await asyncio.sleep(interval_s)
