"""Durable bot orchestration primitives.

This layer coordinates long-running meeting jobs across workers when
``BOT_STATE_BACKEND=redis``. It owns the distributed queue, scheduled starts,
worker leases, and heartbeats. In memory mode it degrades to process-local
no-ops so local development keeps the existing behavior.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from app.config import settings


_WORKER_ID = os.environ.get(
    "BOT_WORKER_ID",
    f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}",
)


@dataclass
class BotHeartbeat:
    bot_id: str
    worker_id: str
    status: Optional[str]
    updated_at: float


class MemoryBotOrchestrator:
    """Process-local implementation used when Redis orchestration is disabled."""

    enabled = False

    def __init__(self) -> None:
        self.worker_id = _WORKER_ID
        self._lock = asyncio.Lock()
        self._leases: dict[str, float] = {}
        self._heartbeats: dict[str, BotHeartbeat] = {}
        self._queue: list[str] = []
        self._scheduled: dict[str, float] = {}

    async def prune_expired(self) -> None:
        now = time.time()
        expired = [bot_id for bot_id, expires in self._leases.items() if expires <= now]
        for bot_id in expired:
            self._leases.pop(bot_id, None)
            self._heartbeats.pop(bot_id, None)

    @asynccontextmanager
    async def scheduler_lock(self) -> AsyncIterator[None]:
        async with self._lock:
            yield

    async def count_active(self) -> int:
        await self.prune_expired()
        return len(self._leases)

    async def try_acquire_lease(self, bot_id: str, ttl_s: Optional[int] = None) -> bool:
        ttl = ttl_s or settings.BOT_WORKER_LEASE_SECONDS
        async with self._lock:
            await self.prune_expired()
            self._leases[bot_id] = time.time() + ttl
            return True

    async def refresh_lease(self, bot_id: str, ttl_s: Optional[int] = None) -> bool:
        ttl = ttl_s or settings.BOT_WORKER_LEASE_SECONDS
        async with self._lock:
            if bot_id not in self._leases:
                return False
            self._leases[bot_id] = time.time() + ttl
            return True

    async def release_lease(self, bot_id: str) -> None:
        async with self._lock:
            self._leases.pop(bot_id, None)
            self._heartbeats.pop(bot_id, None)

    async def cancel_bot(self, bot_id: str) -> None:
        async with self._lock:
            self._queue = [queued_id for queued_id in self._queue if queued_id != bot_id]
            self._scheduled.pop(bot_id, None)
            self._leases.pop(bot_id, None)
            self._heartbeats.pop(bot_id, None)

    async def heartbeat(self, bot_id: str, status: Optional[str] = None) -> bool:
        await self.refresh_lease(bot_id)
        self._heartbeats[bot_id] = BotHeartbeat(
            bot_id=bot_id,
            worker_id=self.worker_id,
            status=status,
            updated_at=time.time(),
        )
        return True

    async def get_heartbeat(self, bot_id: str) -> Optional[BotHeartbeat]:
        return self._heartbeats.get(bot_id)

    async def enqueue(self, bot_id: str) -> int:
        async with self._lock:
            if bot_id not in self._queue:
                self._queue.append(bot_id)
            return self._queue.index(bot_id) + 1

    async def dequeue(self) -> Optional[str]:
        async with self._lock:
            if not self._queue:
                return None
            return self._queue.pop(0)

    async def queue_position(self, bot_id: str) -> Optional[int]:
        async with self._lock:
            try:
                return self._queue.index(bot_id) + 1
            except ValueError:
                return None

    async def schedule(self, bot_id: str, run_at: datetime) -> None:
        async with self._lock:
            self._scheduled[bot_id] = run_at.timestamp()

    async def pop_due_scheduled(self, *, now: Optional[float] = None, limit: int = 100) -> list[str]:
        current = now if now is not None else time.time()
        async with self._lock:
            due = [
                bot_id
                for bot_id, ts in sorted(self._scheduled.items(), key=lambda item: item[1])
                if ts <= current
            ][:limit]
            for bot_id in due:
                self._scheduled.pop(bot_id, None)
            return due

    async def heartbeat_loop(self, bot_id: str, status_getter, *, interval_s: int = 10) -> None:
        while True:
            status = status_getter()
            if not await self.heartbeat(bot_id, status=status):
                return
            await asyncio.sleep(interval_s)


class RedisBotOrchestrator:
    """Redis-backed orchestration for multi-worker bot execution."""

    enabled = True

    def __init__(self, client, *, prefix: str = "jhtl:orch") -> None:
        self._r = client
        self._prefix = prefix
        self.worker_id = _WORKER_ID

    @property
    def _queue_key(self) -> str:
        return f"{self._prefix}:queue"

    @property
    def _queued_set_key(self) -> str:
        return f"{self._prefix}:queued"

    @property
    def _scheduled_key(self) -> str:
        return f"{self._prefix}:scheduled"

    @property
    def _active_key(self) -> str:
        return f"{self._prefix}:active"

    @property
    def _heartbeat_key(self) -> str:
        return f"{self._prefix}:heartbeats"

    @property
    def _scheduler_lock_key(self) -> str:
        return f"{self._prefix}:scheduler-lock"

    def _lease_key(self, bot_id: str) -> str:
        return f"{self._prefix}:lease:{bot_id}"

    async def prune_expired(self) -> None:
        await self._r.zremrangebyscore(self._active_key, 0, time.time())

    @asynccontextmanager
    async def scheduler_lock(self) -> AsyncIterator[None]:
        token = uuid.uuid4().hex
        while True:
            acquired = await self._r.set(
                self._scheduler_lock_key,
                token,
                nx=True,
                ex=max(1, settings.BOT_SCHEDULER_LOCK_SECONDS),
            )
            if acquired:
                break
            await asyncio.sleep(0.1)
        try:
            yield
        finally:
            current = await self._r.get(self._scheduler_lock_key)
            if current == token:
                await self._r.delete(self._scheduler_lock_key)

    async def count_active(self) -> int:
        await self.prune_expired()
        return int(await self._r.zcard(self._active_key))

    async def try_acquire_lease(self, bot_id: str, ttl_s: Optional[int] = None) -> bool:
        ttl = ttl_s or settings.BOT_WORKER_LEASE_SECONDS
        lease_key = self._lease_key(bot_id)
        current_owner = await self._r.get(lease_key)
        if current_owner and current_owner != self.worker_id:
            return False
        if current_owner == self.worker_id:
            await self._r.expire(lease_key, ttl)
        else:
            acquired = await self._r.set(lease_key, self.worker_id, nx=True, ex=ttl)
            if not acquired:
                return False
        await self._r.zadd(self._active_key, {bot_id: time.time() + ttl})
        return True

    async def refresh_lease(self, bot_id: str, ttl_s: Optional[int] = None) -> bool:
        ttl = ttl_s or settings.BOT_WORKER_LEASE_SECONDS
        lease_key = self._lease_key(bot_id)
        if await self._r.get(lease_key) != self.worker_id:
            return False
        await self._r.expire(lease_key, ttl)
        await self._r.zadd(self._active_key, {bot_id: time.time() + ttl})
        return True

    async def release_lease(self, bot_id: str) -> None:
        lease_key = self._lease_key(bot_id)
        if await self._r.get(lease_key) == self.worker_id:
            pipe = self._r.pipeline()
            pipe.delete(lease_key)
            pipe.zrem(self._active_key, bot_id)
            pipe.hdel(self._heartbeat_key, bot_id)
            await pipe.execute()

    async def cancel_bot(self, bot_id: str) -> None:
        pipe = self._r.pipeline()
        pipe.lrem(self._queue_key, 0, bot_id)
        pipe.srem(self._queued_set_key, bot_id)
        pipe.zrem(self._scheduled_key, bot_id)
        pipe.zrem(self._active_key, bot_id)
        pipe.hdel(self._heartbeat_key, bot_id)
        pipe.delete(self._lease_key(bot_id))
        await pipe.execute()

    async def heartbeat(self, bot_id: str, status: Optional[str] = None) -> bool:
        if not await self.refresh_lease(bot_id):
            return False
        payload = {
            "bot_id": bot_id,
            "worker_id": self.worker_id,
            "status": status,
            "updated_at": time.time(),
        }
        await self._r.hset(self._heartbeat_key, bot_id, json.dumps(payload))
        return True

    async def get_heartbeat(self, bot_id: str) -> Optional[BotHeartbeat]:
        raw = await self._r.hget(self._heartbeat_key, bot_id)
        if not raw:
            return None
        data = json.loads(raw)
        return BotHeartbeat(
            bot_id=data["bot_id"],
            worker_id=data["worker_id"],
            status=data.get("status"),
            updated_at=float(data.get("updated_at") or 0),
        )

    async def enqueue(self, bot_id: str) -> int:
        added = await self._r.sadd(self._queued_set_key, bot_id)
        if added:
            await self._r.rpush(self._queue_key, bot_id)
        return await self.queue_position(bot_id) or 0

    async def dequeue(self) -> Optional[str]:
        while True:
            bot_id = await self._r.lpop(self._queue_key)
            if not bot_id:
                return None
            await self._r.srem(self._queued_set_key, bot_id)
            return bot_id

    async def queue_position(self, bot_id: str) -> Optional[int]:
        items = await self._r.lrange(self._queue_key, 0, -1)
        try:
            return items.index(bot_id) + 1
        except ValueError:
            return None

    async def schedule(self, bot_id: str, run_at: datetime) -> None:
        run_at_utc = run_at if run_at.tzinfo else run_at.replace(tzinfo=timezone.utc)
        await self._r.zadd(self._scheduled_key, {bot_id: run_at_utc.timestamp()})

    async def pop_due_scheduled(self, *, now: Optional[float] = None, limit: int = 100) -> list[str]:
        current = now if now is not None else time.time()
        bot_ids = await self._r.zrangebyscore(self._scheduled_key, 0, current, start=0, num=limit)
        if bot_ids:
            await self._r.zrem(self._scheduled_key, *bot_ids)
        return list(bot_ids)

    async def heartbeat_loop(self, bot_id: str, status_getter, *, interval_s: int = 10) -> None:
        while True:
            status = status_getter()
            if not await self.heartbeat(bot_id, status=status):
                return
            await asyncio.sleep(interval_s)


_orchestrator = None


def get_orchestrator():
    """Return the process-wide bot orchestrator."""

    global _orchestrator
    if _orchestrator is not None:
        return _orchestrator
    if settings.BOT_STATE_BACKEND == "redis" and settings.REDIS_URL:
        import redis.asyncio as _aioredis

        client = _aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        _orchestrator = RedisBotOrchestrator(client)
    else:
        _orchestrator = MemoryBotOrchestrator()
    return _orchestrator
