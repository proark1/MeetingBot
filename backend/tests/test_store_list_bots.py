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
    bots, total, _ = await s.list_bots(account_id="acct-1")
    assert total == 3
    assert {b.id for b in bots} == {"a1", "a2", "err"}


async def test_filter_by_status():
    s = await _seed()
    bots, total, _ = await s.list_bots(status="error")
    assert total == 1 and bots[0].id == "err"


async def test_account_id_is_null_filter():
    s = await _seed()
    bots, total, _ = await s.list_bots(account_id_is_null=True)
    assert total == 1 and bots[0].id == "anon"


async def test_sorted_newest_first_and_pagination():
    s = await _seed()
    page1, total, _ = await s.list_bots(limit=2, offset=0)
    assert total == 5
    # newest first by created_at: err(day5) > anon(day4) ...
    assert [b.id for b in page1] == ["err", "anon"]
    page2, _, _ = await s.list_bots(limit=2, offset=2)
    assert [b.id for b in page2] == ["a2", "b1"]


async def test_sub_user_filter():
    s = Store()
    await s.create_bot(_bot("s1", account_id="acct", sub_user_id="u1"))
    await s.create_bot(_bot("s2", account_id="acct", sub_user_id="u2"))
    bots, total, _ = await s.list_bots(account_id="acct", sub_user_id="u1")
    assert total == 1 and bots[0].id == "s1"


async def test_cursor_pagination_full_traversal():
    """Cursor-based pagination should traverse all bots without duplicates or gaps."""
    s = await _seed()
    seen = []
    cursor = None
    while True:
        page, total, next_cursor = await s.list_bots(limit=2, after_cursor=cursor)
        seen.extend(b.id for b in page)
        assert total == 5
        if next_cursor is None:
            break
        cursor = next_cursor
    assert len(seen) == 5
    assert len(set(seen)) == 5


async def test_cursor_matches_offset_ordering():
    """Page 2 via cursor must match page 2 via offset."""
    s = await _seed()
    _, _, next_cursor = await s.list_bots(limit=2)
    cursor_page, _, _ = await s.list_bots(limit=2, after_cursor=next_cursor)
    offset_page, _, _ = await s.list_bots(limit=2, offset=2)
    assert [b.id for b in cursor_page] == [b.id for b in offset_page]


async def test_cursor_no_next_on_last_page():
    """next_cursor must be None when the last page is returned."""
    s = await _seed()
    _, _, next_cursor = await s.list_bots(limit=100)
    assert next_cursor is None


async def test_invalid_cursor_falls_back_to_first_page():
    """A malformed cursor should not crash — returns first page."""
    s = await _seed()
    bots, total, _ = await s.list_bots(limit=2, after_cursor="not-valid-base64!!!")
    assert total == 5
    assert len(bots) == 2
