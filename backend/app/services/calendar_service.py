"""Calendar auto-join — poll an iCal feed and schedule bots for upcoming meetings.

Set CALENDAR_ICAL_URL in the environment to an iCal URL (e.g. Google Calendar's
"Secret address in iCal format" from calendar settings, or any Outlook / iCloud
calendar URL).  The scheduler calls `sync_calendar` every 5 minutes.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

# How far ahead to look for meetings to auto-join (minutes)
_LOOKAHEAD_MINUTES = 15
# Minimum notice before a meeting to create a bot (minutes)
_MIN_NOTICE_MINUTES = 1


async def sync_calendar(db_factory) -> None:
    """Fetch the iCal feed and create bots for meetings starting soon."""
    from app.config import settings

    if not settings.CALENDAR_ICAL_URL:
        return

    try:
        events = await _fetch_upcoming_events(settings.CALENDAR_ICAL_URL)
    except Exception as exc:
        logger.error("Calendar sync failed (fetch): %s", exc)
        return

    if not events:
        return

    async with db_factory() as db:
        from sqlalchemy import select
        from app.models.bot import Bot

        for event in events:
            meeting_url = event.get("url")
            if not meeting_url:
                continue

            start_dt: datetime = event["start"]
            now = datetime.now(timezone.utc)
            delay_s = (start_dt - now).total_seconds()

            if delay_s < -60:
                # Already started more than 1 min ago — skip
                continue

            # Deduplicate: skip if a bot was already created for this URL + start time
            window_start = start_dt - timedelta(minutes=5)
            window_end = start_dt + timedelta(minutes=5)
            existing = (
                await db.execute(
                    select(Bot).where(
                        Bot.meeting_url == meeting_url,
                        Bot.created_at >= window_start,
                        Bot.created_at <= window_end,
                    )
                )
            ).scalar_one_or_none()

            if existing:
                logger.debug(
                    "Calendar: bot already exists for %s at %s", meeting_url, start_dt
                )
                continue

            from app.models.bot import Bot as BotModel
            from app.services.bot_service import detect_platform
            from app.database import AsyncSessionLocal
            import asyncio
            from app.api.bots import _running_tasks
            from app.services import bot_service
            import secrets

            bot = BotModel(
                meeting_url=meeting_url,
                meeting_platform=detect_platform(meeting_url),
                bot_name=settings.BOT_NAME_DEFAULT,
                join_at=start_dt,
                status="scheduled",
                share_token=secrets.token_urlsafe(24),
                extra_metadata={"source": "calendar_auto_join", "event_title": event.get("title", "")},
            )
            db.add(bot)
            await db.commit()
            await db.refresh(bot)

            task = asyncio.create_task(
                bot_service.run_bot_lifecycle(bot.id, AsyncSessionLocal)
            )
            _running_tasks[bot.id] = task
            task.add_done_callback(lambda _t, bid=bot.id: _running_tasks.pop(bid, None))

            logger.info(
                "Calendar: scheduled bot %s for '%s' at %s (%.0f s from now)",
                bot.id, event.get("title", "?"), start_dt.isoformat(), max(delay_s, 0),
            )


async def _fetch_upcoming_events(ical_url: str) -> list[dict]:
    """Download an iCal feed and return events starting within the lookahead window."""
    try:
        from icalendar import Calendar  # type: ignore
    except ImportError:
        raise RuntimeError(
            "icalendar is not installed — run: pip install icalendar"
        )

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(ical_url)
        resp.raise_for_status()
        raw = resp.content

    cal = Calendar.from_ical(raw)
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=_LOOKAHEAD_MINUTES)

    events: list[dict] = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        dtstart = component.get("DTSTART")
        if dtstart is None:
            continue

        start_dt = dtstart.dt
        # Normalise to datetime with UTC timezone
        if isinstance(start_dt, datetime):
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        else:
            # date-only — treat as midnight UTC
            start_dt = datetime(start_dt.year, start_dt.month, start_dt.day,
                                tzinfo=timezone.utc)

        if not (now - timedelta(minutes=1) <= start_dt <= horizon):
            continue

        # Extract meeting URL from description, location, or URL field
        meeting_url = _extract_meeting_url(component)
        if not meeting_url:
            continue

        events.append({
            "title": str(component.get("SUMMARY", "")),
            "start": start_dt,
            "url": meeting_url,
        })

    return events


_MEETING_URL_PATTERNS = (
    "zoom.us/j/", "zoom.us/my/", "zoom.com/j/",
    "meet.google.com/",
    "teams.microsoft.com/l/meetup-join", "teams.live.com/meet/",
    "webex.com/", "whereby.com/", "bluejeans.com/", "gotomeeting.com/",
)


def _extract_meeting_url(component) -> str | None:
    """Scan VEVENT fields for a recognisable video-meeting URL."""
    import re
    url_re = re.compile(r'https?://\S+', re.IGNORECASE)

    candidates: list[str] = []

    for field in ("URL", "LOCATION", "DESCRIPTION"):
        val = component.get(field)
        if val is None:
            continue
        text = str(val)
        found = url_re.findall(text)
        candidates.extend(found)

    for url in candidates:
        clean = url.rstrip(".,;)")
        if any(pat in clean for pat in _MEETING_URL_PATTERNS):
            return clean

    return None
