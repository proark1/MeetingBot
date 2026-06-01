"""Unit tests for the stuck-bot reaper (bot_service.reap_stuck_bots).

The reaper is a defence-in-depth safety net: it force-terminates bots that have
been actively running past their hard wall-clock ceiling so a hung or orphaned
lifecycle can't occupy a concurrency slot forever.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.store import Store, BotSession
from app.services import bot_service


def _bot(bot_id, status, started_at=None, created_at=None):
    return BotSession(
        id=bot_id,
        meeting_url="https://meet.example/x",
        meeting_platform="google_meet",
        bot_name="b",
        status=status,
        started_at=started_at,
        created_at=created_at or datetime.now(timezone.utc),
    )


async def test_reaper_forces_terminal_on_orphaned_active_bot(app, monkeypatch):
    """An active bot with no live task and an old start time is force-errored."""
    s = Store()
    old = datetime.now(timezone.utc) - timedelta(seconds=20_000)
    now = datetime.now(timezone.utc)
    await s.create_bot(_bot("stuck", "in_call", started_at=old))
    await s.create_bot(_bot("fresh", "in_call", started_at=now))
    # Old but not actively running — a scheduled bot can legitimately wait days.
    await s.create_bot(_bot("sched", "scheduled", created_at=old))
    monkeypatch.setattr(bot_service, "store", s)

    reaped = await bot_service.reap_stuck_bots(max_age_seconds=10_800)

    assert reaped == 1
    stuck = await s.get_bot("stuck")
    assert stuck.status == "error"
    assert "lifetime" in (stuck.error_message or "").lower()
    # The recent and the scheduled bots are untouched.
    assert (await s.get_bot("fresh")).status == "in_call"
    assert (await s.get_bot("sched")).status == "scheduled"


async def test_reaper_cancels_overdue_running_task(app, monkeypatch):
    """An overdue bot whose lifecycle task is still alive gets cancelled
    (so the task's own salvage path runs) rather than force-errored."""
    s = Store()
    old = datetime.now(timezone.utc) - timedelta(seconds=20_000)
    await s.create_bot(_bot("running", "transcribing", started_at=old))
    monkeypatch.setattr(bot_service, "store", s)

    async def _never():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never())
    from app.api import bots as bots_mod
    monkeypatch.setitem(bots_mod._running_tasks, "running", task)

    reaped = await bot_service.reap_stuck_bots(max_age_seconds=10_800)

    assert reaped == 1
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_reaper_noop_when_nothing_stuck(app, monkeypatch):
    s = Store()
    now = datetime.now(timezone.utc)
    await s.create_bot(_bot("a", "in_call", started_at=now))
    await s.create_bot(_bot("b", "joining", created_at=now))
    monkeypatch.setattr(bot_service, "store", s)

    assert await bot_service.reap_stuck_bots(max_age_seconds=10_800) == 0
