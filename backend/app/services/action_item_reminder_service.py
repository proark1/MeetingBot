"""Action-item due-date reminders.

Closes the loop on extracted action items: a background scan finds open items
whose ``due_date`` is approaching ("due_soon") or has passed ("overdue") and
dispatches a webhook event so integrations can notify the assignee. Each stage
fires at most once per item (tracked via ``reminder_stage``), and stages only
ever advance ``due_soon → overdue``.

``due_date`` is free text (the model may emit "2026-05-18", "next Friday", …),
so it's parsed defensively — only confidently-parseable calendar dates trigger
reminders; anything else is skipped (logged at debug), never guessed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Reminder stages in ascending order, so we never regress a fired stage.
_STAGE_ORDER = {None: 0, "due_soon": 1, "overdue": 2}

# Accepted explicit date formats (date-only and a couple of common ones).
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y")


def parse_due_date(raw: Optional[str]) -> Optional[date]:
    """Parse a free-text due_date into a calendar date, or None if not confident.

    Handles ISO-ish and a few common explicit formats. Relative phrases
    ("next Friday", "EOW") are intentionally NOT guessed — they return None so
    no misleading reminder fires.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Try a full ISO datetime first (e.g. "2026-05-18T00:00:00Z").
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    logger.debug("action-item reminder: unparseable due_date %r — skipping", raw)
    return None


def classify_stage(due: date, now: Optional[datetime] = None, due_soon_hours: int = 24) -> Optional[str]:
    """Return the reminder stage warranted by a due date: 'overdue', 'due_soon', or None."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if due < today:
        return "overdue"
    # Within the due-soon window (due date is today or within N hours)?
    soon_cutoff = (now + timedelta(hours=due_soon_hours)).date()
    if due <= soon_cutoff:
        return "due_soon"
    return None


def _stage_advances(current: Optional[str], candidate: str) -> bool:
    """True if ``candidate`` is a strictly higher stage than ``current``."""
    return _STAGE_ORDER.get(candidate, 0) > _STAGE_ORDER.get(current, 0)


async def scan_and_dispatch(now: Optional[datetime] = None) -> int:
    """Scan open action items and dispatch due_soon/overdue reminders.

    Returns the number of reminder events dispatched. Each item advances at most
    one stage per scan and never re-fires a stage it's already at.
    """
    from app.config import settings
    from app.db import AsyncSessionLocal
    from app.models.account import ActionItem
    from app.services import webhook_service
    from sqlalchemy import select, or_

    now = now or datetime.now(timezone.utc)
    due_soon_hours = settings.ACTION_ITEM_DUE_SOON_HOURS
    dispatched = 0

    async with AsyncSessionLocal() as db:
        # Skip items already at the terminal "overdue" stage: they can never
        # advance further (stages only go None → due_soon → overdue), so they
        # would be re-scanned and re-skipped every cycle forever, growing the
        # working set without bound as open-but-overdue items accumulate.
        # NULL stages MUST stay included (a plain `!= "overdue"` would drop them
        # since SQL `!=` is NULL for NULL operands).
        result = await db.execute(
            select(ActionItem).where(
                ActionItem.status == "open",
                ActionItem.due_date.isnot(None),
                or_(
                    ActionItem.reminder_stage.is_(None),
                    ActionItem.reminder_stage != "overdue",
                ),
            )
        )
        items = result.scalars().all()

        to_notify: list[tuple[ActionItem, str]] = []
        for item in items:
            due = parse_due_date(item.due_date)
            if due is None:
                continue
            stage = classify_stage(due, now=now, due_soon_hours=due_soon_hours)
            if stage is None:
                continue
            if not _stage_advances(item.reminder_stage, stage):
                continue
            item.reminder_stage = stage
            item.reminder_sent_at = now
            to_notify.append((item, stage))

        if to_notify:
            await db.commit()

    # Dispatch outside the DB session.
    for item, stage in to_notify:
        try:
            await webhook_service.dispatch_event(
                f"action_item.{stage}",
                {
                    "action_item_id": item.id,
                    "account_id": item.account_id,
                    "bot_id": item.bot_id,
                    "task": item.task,
                    "assignee": item.assignee,
                    "due_date": item.due_date,
                    "status": item.status,
                    "stage": stage,
                },
                account_id=item.account_id,
            )
            dispatched += 1
        except Exception as exc:
            logger.warning("action-item reminder dispatch failed for %s: %s", item.id, exc)

    if dispatched:
        logger.info("Action-item reminders: dispatched %d event(s)", dispatched)
    return dispatched


async def reminder_loop() -> None:
    """Background loop: periodically scan for due/overdue action items."""
    import asyncio
    from app.config import settings

    interval = max(60, settings.ACTION_ITEM_REMINDER_INTERVAL_S)
    while True:
        try:
            await scan_and_dispatch()
        except Exception:
            logger.exception("Action-item reminder loop iteration failed")
        await asyncio.sleep(interval)
