"""Interface for live bot-lifecycle state (Phase 0 of distributed-state work).

The in-memory ``Store`` singleton implements this protocol today. Extracting the
bot-state surface as an explicit contract lets a future shared/distributed
backend (e.g. Redis/Postgres-backed, so multiple API workers can serve the same
live meeting) be swapped in without touching call sites.

Scope is deliberately just the *bot state* operations — the part that would have
to become distributed. Webhook persistence, snapshot writes, and the cleanup
loop live on ``Store`` separately and are already DB-backed, so they're out of
scope here.

Note: the runtime handles of an active bot (the Playwright ``page``, asyncio
tasks, PulseAudio routing) are NOT part of this contract — they are inherently
process-local to the worker running the bot's browser and can never be
serialized into a shared store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.store import BotSession


@runtime_checkable
class BotStateStore(Protocol):
    """Read/write contract for active bot sessions."""

    async def create_bot(self, session: "BotSession") -> None: ...

    async def get_bot(self, bot_id: str) -> "Optional[BotSession]": ...

    async def update_bot(self, bot_id: str, **kwargs) -> "Optional[BotSession]": ...

    async def get_bot_by_share_hash(self, share_hash: str) -> "Optional[BotSession]": ...

    async def list_bots(
        self,
        status: Optional[str] = ...,
        limit: int = ...,
        offset: int = ...,
        account_id: Optional[str] = ...,
        sub_user_id: Optional[str] = ...,
        account_id_is_null: bool = ...,
        after_cursor: Optional[str] = ...,
    ) -> "tuple[list[BotSession], int, Optional[str]]": ...

    async def delete_bot(self, bot_id: str) -> None: ...

    async def list_live_bots(self, statuses) -> "list[BotSession]":
        """Snapshot of live bots whose status is in ``statuses`` (used by the
        stuck-bot reaper). ``statuses`` is any container supporting ``in``."""
        ...
