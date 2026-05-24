"""Redis-backed ``BotStateStore`` (Phase 1 of distributed live-state).

Stores each bot's serializable state (``BotSession.to_state_dict``) as a JSON
string, with two helper structures so the ``BotStateStore`` query surface keeps
working across workers:

  * ``{prefix}:bot:{id}``     — the bot's JSON state
  * ``{prefix}:bots:index``   — sorted set, score = created_at epoch (newest-first listing)
  * ``{prefix}:bots:share``   — hash, share_token_hash -> bot id (O(1) /share lookup)

Runtime handles (Playwright page, asyncio primitives) are NOT shared — they're
process-local to the worker running the bot's browser. A bot reconstructed on a
different worker has live state but no handles; cross-worker control routing is
later work (pub/sub), not this layer.

The client is injected, so tests drive it with ``fakeredis``. Construct it via
``app.store.get_bot_state_store`` when ``BOT_STATE_BACKEND=redis``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from app.store import BotSession, Store, _now


class RedisBotStateStore:
    """Implements the ``BotStateStore`` protocol against Redis."""

    def __init__(self, client, *, prefix: str = "jhtl") -> None:
        self._r = client
        self._prefix = prefix

    def _bot_key(self, bot_id: str) -> str:
        return f"{self._prefix}:bot:{bot_id}"

    @property
    def _index_key(self) -> str:
        return f"{self._prefix}:bots:index"

    @property
    def _share_key(self) -> str:
        return f"{self._prefix}:bots:share"

    @staticmethod
    def _score(session: BotSession) -> float:
        ca = session.created_at
        return ca.timestamp() if isinstance(ca, datetime) else 0.0

    async def create_bot(self, session: BotSession) -> None:
        pipe = self._r.pipeline()
        pipe.set(self._bot_key(session.id), json.dumps(session.to_state_dict()))
        pipe.zadd(self._index_key, {session.id: self._score(session)})
        if session.share_token_hash:
            pipe.hset(self._share_key, session.share_token_hash, session.id)
        await pipe.execute()

    async def get_bot(self, bot_id: str) -> Optional[BotSession]:
        raw = await self._r.get(self._bot_key(bot_id))
        if not raw:
            return None
        return BotSession.from_state_dict(json.loads(raw))

    async def update_bot(self, bot_id: str, **kwargs) -> Optional[BotSession]:
        forbidden = set(kwargs) & Store._IMMUTABLE_BOT_FIELDS
        if forbidden:
            raise ValueError(
                f"update_bot cannot mutate immutable field(s): {sorted(forbidden)!r}"
            )
        key = self._bot_key(bot_id)
        from redis.exceptions import WatchError

        async with self._r.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if not raw:
                        await pipe.unwatch()
                        return None
                    bot = BotSession.from_state_dict(json.loads(raw))
                    old_share = bot.share_token_hash
                    for k, v in kwargs.items():
                        setattr(bot, k, v)
                    bot.updated_at = _now()

                    pipe.multi()
                    pipe.set(key, json.dumps(bot.to_state_dict()))
                    if "share_token_hash" in kwargs:
                        if old_share:
                            pipe.hdel(self._share_key, old_share)
                        if bot.share_token_hash:
                            pipe.hset(self._share_key, bot.share_token_hash, bot_id)
                    await pipe.execute()
                    return bot
                except WatchError:
                    continue  # another writer changed the key — re-read and retry

    async def get_bot_by_share_hash(self, share_hash: str) -> Optional[BotSession]:
        bot_id = await self._r.hget(self._share_key, share_hash)
        if not bot_id:
            return None
        return await self.get_bot(bot_id)

    async def list_bots(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        account_id: Optional[str] = None,
        sub_user_id: Optional[str] = None,
        account_id_is_null: bool = False,
    ) -> "tuple[list[BotSession], int]":
        # Newest-first via the created_at-scored index, then filter in Python
        # (parity with the in-memory store, which also scans the full set).
        ids = await self._r.zrevrange(self._index_key, 0, -1)
        if not ids:
            return [], 0
        raws = await self._r.mget([self._bot_key(i) for i in ids])
        bots: list[BotSession] = []
        for raw in raws:
            if not raw:
                continue  # index entry whose bot key was deleted
            b = BotSession.from_state_dict(json.loads(raw))
            if status and b.status != status:
                continue
            if account_id and b.account_id != account_id:
                continue
            if account_id_is_null and b.account_id is not None:
                continue
            if sub_user_id is not None and b.sub_user_id != sub_user_id:
                continue
            bots.append(b)
        total = len(bots)
        return bots[offset:offset + limit], total

    async def delete_bot(self, bot_id: str) -> None:
        bot = await self.get_bot(bot_id)
        pipe = self._r.pipeline()
        pipe.delete(self._bot_key(bot_id))
        pipe.zrem(self._index_key, bot_id)
        if bot and bot.share_token_hash:
            pipe.hdel(self._share_key, bot.share_token_hash)
        await pipe.execute()
