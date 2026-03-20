"""Server-Sent Events (SSE) subscription manager for live transcript streaming.

Maintains a registry of asyncio.Queue per bot_id. The live transcript loop
in bot_service calls `push_entry` for each new transcript entry, and SSE
handler endpoints yield from these queues.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# bot_id → list of subscriber queues
_subscriptions: dict[str, list[asyncio.Queue]] = {}
_lock = asyncio.Lock()

# Terminal statuses — SSE stream ends when bot reaches one of these
TERMINAL_STATUSES = {"done", "error", "cancelled"}


async def subscribe(bot_id: str) -> asyncio.Queue:
    """Create a new subscriber queue for a bot's live transcript."""
    async with _lock:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        _subscriptions.setdefault(bot_id, []).append(q)
    return q


async def unsubscribe(bot_id: str, q: asyncio.Queue) -> None:
    """Remove a subscriber queue (called when client disconnects)."""
    async with _lock:
        subs = _subscriptions.get(bot_id, [])
        try:
            subs.remove(q)
        except ValueError:
            pass
        if not subs:
            _subscriptions.pop(bot_id, None)


async def push_entry(bot_id: str, entry: dict) -> None:
    """Push a live transcript entry to all subscribers of a bot."""
    async with _lock:
        subs = list(_subscriptions.get(bot_id, []))
    for q in subs:
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            logger.warning("SSE queue full for bot %s — dropping entry", bot_id)


async def push_terminal(bot_id: str, status: str) -> None:
    """Push a terminal event (done/error/cancelled) to all subscribers."""
    await push_entry(bot_id, {"__terminal__": True, "status": status})
