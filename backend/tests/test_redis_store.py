"""Tests for RedisBotStateStore, driven by fakeredis (no real Redis needed)."""

from datetime import datetime, timezone

import pytest

from app.store import BotSession, Store
from app.store_interface import BotStateStore

fakeredis = pytest.importorskip("fakeredis")


@pytest.fixture
def rstore():
    from app.redis_store import RedisBotStateStore
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    return RedisBotStateStore(client, prefix="test")


def _bot(bot_id, status="done", account_id=None, sub_user_id=None, day=1, share=None):
    return BotSession(
        id=bot_id, meeting_url="https://meet.example/x",
        meeting_platform="google_meet", bot_name="b", status=status,
        account_id=account_id, sub_user_id=sub_user_id,
        share_token_hash=share,
        created_at=datetime(2026, 5, day, tzinfo=timezone.utc),
    )


def test_conforms_to_protocol(rstore):
    assert isinstance(rstore, BotStateStore)


async def test_create_get_delete(rstore):
    await rstore.create_bot(_bot("a1", account_id="acct"))
    got = await rstore.get_bot("a1")
    assert got is not None and got.id == "a1" and got.account_id == "acct"
    assert isinstance(got.created_at, datetime)

    await rstore.delete_bot("a1")
    assert await rstore.get_bot("a1") is None


async def test_get_missing_returns_none(rstore):
    assert await rstore.get_bot("nope") is None


async def test_update_bot_mutates_and_bumps_updated_at(rstore):
    await rstore.create_bot(_bot("a1", status="in_call"))
    before = await rstore.get_bot("a1")
    updated = await rstore.update_bot("a1", status="done", health_score=88)
    assert updated.status == "done" and updated.health_score == 88
    assert (await rstore.get_bot("a1")).status == "done"
    assert updated.updated_at >= before.updated_at


async def test_update_missing_returns_none(rstore):
    assert await rstore.update_bot("ghost", status="done") is None


async def test_update_rejects_immutable_fields(rstore):
    await rstore.create_bot(_bot("a1", account_id="acct"))
    with pytest.raises(ValueError):
        await rstore.update_bot("a1", account_id="other")


async def test_share_hash_index(rstore):
    await rstore.create_bot(_bot("a1", share="hash-abc"))
    found = await rstore.get_bot_by_share_hash("hash-abc")
    assert found is not None and found.id == "a1"
    assert await rstore.get_bot_by_share_hash("missing") is None

    # Re-pointing the share hash via update keeps the index in sync.
    await rstore.create_bot(_bot("a2"))
    await rstore.update_bot("a2", share_token_hash="hash-abc")
    assert (await rstore.get_bot_by_share_hash("hash-abc")).id == "a2"


async def test_list_bots_filter_sort_paginate(rstore):
    await rstore.create_bot(_bot("a1", account_id="acct-1", day=1))
    await rstore.create_bot(_bot("a2", account_id="acct-1", day=3))
    await rstore.create_bot(_bot("b1", account_id="acct-2", day=2))
    await rstore.create_bot(_bot("anon", account_id=None, day=4))
    await rstore.create_bot(_bot("err", account_id="acct-1", status="error", day=5))

    by_acct, total, _ = await rstore.list_bots(account_id="acct-1")
    assert total == 3 and {b.id for b in by_acct} == {"a1", "a2", "err"}

    errs, terr, _ = await rstore.list_bots(status="error")
    assert terr == 1 and errs[0].id == "err"

    anon, tanon, _ = await rstore.list_bots(account_id_is_null=True)
    assert tanon == 1 and anon[0].id == "anon"

    # newest-first ordering by created_at + pagination
    page1, total_all, _ = await rstore.list_bots(limit=2, offset=0)
    assert total_all == 5 and [b.id for b in page1] == ["err", "anon"]
    page2, _, _ = await rstore.list_bots(limit=2, offset=2)
    assert [b.id for b in page2] == ["a2", "b1"]


async def test_delete_clears_index_and_share(rstore):
    await rstore.create_bot(_bot("a1", share="h1"))
    await rstore.delete_bot("a1")
    bots, total, _ = await rstore.list_bots()
    assert total == 0 and bots == []
    assert await rstore.get_bot_by_share_hash("h1") is None


async def test_parity_with_in_memory_store_filtering(rstore):
    # The Redis store's list_bots should return the same ids as the in-memory
    # Store for the same data + filter (ordering and membership).
    mem = Store()
    bots = [
        _bot("a1", account_id="acct", day=1),
        _bot("a2", account_id="acct", day=4),
        _bot("b1", account_id="other", day=3),
    ]
    for b in bots:
        await mem.create_bot(b)
        await rstore.create_bot(b)

    mem_list, mem_total, _ = await mem.list_bots(account_id="acct")
    r_list, r_total, _ = await rstore.list_bots(account_id="acct")
    assert mem_total == r_total
    assert [b.id for b in mem_list] == [b.id for b in r_list]


async def test_list_live_bots_filters_by_status(rstore):
    await rstore.create_bot(_bot("j", status="joining"))
    await rstore.create_bot(_bot("c", status="in_call"))
    await rstore.create_bot(_bot("d", status="done"))
    live = await rstore.list_live_bots({"joining", "in_call"})
    assert {b.id for b in live} == {"j", "c"}


async def test_list_live_bots_parity_with_memory(rstore):
    mem = Store()
    for b in (_bot("j", status="joining"), _bot("d", status="done")):
        await mem.create_bot(b)
        await rstore.create_bot(b)
    statuses = {"joining", "in_call", "call_ended", "transcribing"}
    assert {b.id for b in await mem.list_live_bots(statuses)} == \
           {b.id for b in await rstore.list_live_bots(statuses)}
