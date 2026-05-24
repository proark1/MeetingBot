"""Unit tests for Store.list_bots filtering/pagination (uses a fresh Store)."""

from datetime import datetime, timezone

from app.store import Store, BotSession


def _bot(bot_id, status="done", account_id=None, sub_user_id=None, day=1):
    return BotSession(
        id=bot_id,
        meeting_url="https://meet.example/x",
        meeting_platform="google_meet",
        bot_name="b",
        status=status,
        account_id=account_id,
        sub_user_id=sub_user_id,
        created_at=datetime(2026, 5, day, tzinfo=timezone.utc),
    )


async def _seed():
    s = Store()
    await s.create_bot(_bot("a1", account_id="acct-1", day=1))
    await s.create_bot(_bot("a2", account_id="acct-1", day=3))
    await s.create_bot(_bot("b1", account_id="acct-2", day=2))
    await s.create_bot(_bot("anon", account_id=None, day=4))
    await s.create_bot(_bot("err", account_id="acct-1", status="error", day=5))
    return s


async def test_filter_by_account():
    s = await _seed()
    bots, total = await s.list_bots(account_id="acct-1")
    assert total == 3
    assert {b.id for b in bots} == {"a1", "a2", "err"}


async def test_filter_by_status():
    s = await _seed()
    bots, total = await s.list_bots(status="error")
    assert total == 1 and bots[0].id == "err"


async def test_account_id_is_null_filter():
    s = await _seed()
    bots, total = await s.list_bots(account_id_is_null=True)
    assert total == 1 and bots[0].id == "anon"


async def test_sorted_newest_first_and_pagination():
    s = await _seed()
    page1, total = await s.list_bots(limit=2, offset=0)
    assert total == 5
    # newest first by created_at: err(day5) > anon(day4) ...
    assert [b.id for b in page1] == ["err", "anon"]
    page2, _ = await s.list_bots(limit=2, offset=2)
    assert [b.id for b in page2] == ["a2", "b1"]


async def test_sub_user_filter():
    s = Store()
    await s.create_bot(_bot("s1", account_id="acct", sub_user_id="u1"))
    await s.create_bot(_bot("s2", account_id="acct", sub_user_id="u2"))
    bots, total = await s.list_bots(account_id="acct", sub_user_id="u1")
    assert total == 1 and bots[0].id == "s1"
