"""Tests for durable bot orchestration primitives."""

from datetime import datetime, timedelta, timezone

import pytest

fakeredis = pytest.importorskip("fakeredis")


@pytest.fixture
def redis_orchestrator():
    from app.services.orchestration_service import RedisBotOrchestrator

    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    return RedisBotOrchestrator(client, prefix="test:orch")


async def test_redis_queue_is_fifo_and_deduped(redis_orchestrator):
    assert await redis_orchestrator.enqueue("bot-a") == 1
    assert await redis_orchestrator.enqueue("bot-b") == 2
    assert await redis_orchestrator.enqueue("bot-a") == 1

    assert await redis_orchestrator.dequeue() == "bot-a"
    assert await redis_orchestrator.dequeue() == "bot-b"
    assert await redis_orchestrator.dequeue() is None


async def test_redis_scheduled_pop_due_only(redis_orchestrator):
    now = datetime.now(timezone.utc)
    await redis_orchestrator.schedule("due", now - timedelta(seconds=1))
    await redis_orchestrator.schedule("later", now + timedelta(hours=1))

    due = await redis_orchestrator.pop_due_scheduled(now=now.timestamp())

    assert due == ["due"]
    assert await redis_orchestrator.pop_due_scheduled(now=now.timestamp()) == []


async def test_redis_lease_heartbeat_and_release(redis_orchestrator):
    assert await redis_orchestrator.try_acquire_lease("bot-a", ttl_s=30) is True
    assert await redis_orchestrator.count_active() == 1

    await redis_orchestrator.heartbeat("bot-a", status="in_call")
    hb = await redis_orchestrator.get_heartbeat("bot-a")

    assert hb is not None
    assert hb.bot_id == "bot-a"
    assert hb.status == "in_call"
    assert hb.worker_id == redis_orchestrator.worker_id

    await redis_orchestrator.release_lease("bot-a")
    assert await redis_orchestrator.count_active() == 0
    assert await redis_orchestrator.get_heartbeat("bot-a") is None


async def test_redis_cancel_removes_all_orchestration_state(redis_orchestrator):
    now = datetime.now(timezone.utc)
    await redis_orchestrator.enqueue("bot-a")
    await redis_orchestrator.schedule("bot-a", now)
    await redis_orchestrator.try_acquire_lease("bot-a", ttl_s=30)
    await redis_orchestrator.heartbeat("bot-a", status="queued")

    await redis_orchestrator.cancel_bot("bot-a")

    assert await redis_orchestrator.dequeue() is None
    assert await redis_orchestrator.pop_due_scheduled(now=now.timestamp() + 1) == []
    assert await redis_orchestrator.count_active() == 0
    assert await redis_orchestrator.get_heartbeat("bot-a") is None
