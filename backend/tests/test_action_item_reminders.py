"""Tests for action-item due-date reminders."""
import uuid
from datetime import datetime, timezone, timedelta, date

import pytest

from app.db import AsyncSessionLocal
from app.models.account import ActionItem
from app.services import action_item_reminder_service as svc
from sqlalchemy import select


# ── Pure logic ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("2026-05-18", date(2026, 5, 18)),
    ("2026/05/18", date(2026, 5, 18)),
    ("2026-05-18T09:00:00Z", date(2026, 5, 18)),
    ("May 18, 2026", date(2026, 5, 18)),
    ("next Friday", None),     # relative — must NOT be guessed
    ("EOW", None),
    ("", None),
    (None, None),
])
def test_parse_due_date(raw, expected):
    assert svc.parse_due_date(raw) == expected


def test_classify_stage():
    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    assert svc.classify_stage(date(2026, 5, 17), now=now) == "overdue"
    assert svc.classify_stage(date(2026, 5, 18), now=now) == "due_soon"   # due today
    assert svc.classify_stage(date(2026, 5, 19), now=now, due_soon_hours=24) == "due_soon"
    assert svc.classify_stage(date(2026, 5, 25), now=now, due_soon_hours=24) is None  # far off


def test_stage_never_regresses():
    assert svc._stage_advances(None, "due_soon") is True
    assert svc._stage_advances("due_soon", "overdue") is True
    assert svc._stage_advances("overdue", "due_soon") is False
    assert svc._stage_advances("due_soon", "due_soon") is False


# ── Scan + dispatch ─────────────────────────────────────────────────────────────

async def _add_item(*, due_date, status="open", reminder_stage=None) -> str:
    async with AsyncSessionLocal() as db:
        item = ActionItem(
            account_id="acct-1",
            bot_id="bot-1",
            content_hash=uuid.uuid4().hex,
            task="Ship the thing",
            assignee="Alice",
            due_date=due_date,
            status=status,
            reminder_stage=reminder_stage,
        )
        db.add(item)
        await db.commit()
        return item.id


async def _stage(item_id: str):
    async with AsyncSessionLocal() as db:
        row = await db.execute(select(ActionItem.reminder_stage).where(ActionItem.id == item_id))
        return row.scalar_one()


@pytest.mark.asyncio
async def test_scan_dispatches_and_marks_stage(app, monkeypatch):
    sent = []

    async def _fake_dispatch(event, payload, **kwargs):
        sent.append((event, payload))

    import app.services.webhook_service as _ws
    monkeypatch.setattr(_ws, "dispatch_event", _fake_dispatch)

    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    overdue_id = await _add_item(due_date="2026-05-10")          # overdue
    soon_id = await _add_item(due_date="2026-05-18")             # due today
    far_id = await _add_item(due_date="2030-01-01")             # far off — no reminder
    unparseable_id = await _add_item(due_date="sometime soon")  # skipped

    count = await svc.scan_and_dispatch(now=now)

    assert count == 2
    events = {e for e, _ in sent}
    assert events == {"action_item.overdue", "action_item.due_soon"}
    assert await _stage(overdue_id) == "overdue"
    assert await _stage(soon_id) == "due_soon"
    assert await _stage(far_id) is None
    assert await _stage(unparseable_id) is None


@pytest.mark.asyncio
async def test_scan_is_idempotent_per_stage(app, monkeypatch):
    sent = []

    async def _fake_dispatch(event, payload, **kwargs):
        sent.append(event)

    import app.services.webhook_service as _ws
    monkeypatch.setattr(_ws, "dispatch_event", _fake_dispatch)

    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    await _add_item(due_date="2026-05-10")  # overdue

    first = await svc.scan_and_dispatch(now=now)
    second = await svc.scan_and_dispatch(now=now)

    assert first == 1
    assert second == 0          # already at "overdue" — no re-fire
    assert sent == ["action_item.overdue"]


@pytest.mark.asyncio
async def test_done_items_are_ignored(app, monkeypatch):
    async def _fake_dispatch(event, payload, **kwargs):
        raise AssertionError("should not dispatch for a done item")

    import app.services.webhook_service as _ws
    monkeypatch.setattr(_ws, "dispatch_event", _fake_dispatch)

    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    await _add_item(due_date="2026-05-10", status="done")
    count = await svc.scan_and_dispatch(now=now)
    assert count == 0
