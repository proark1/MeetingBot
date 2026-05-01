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


# ── Auxiliary channels (coaching, analytics, decisions, agentic) ────────────────
# Separate from the main transcript channel so different clients can subscribe
# to only the streams they care about. Each channel is keyed by (channel, bot_id).
_aux_subscriptions: dict[tuple[str, str], list[asyncio.Queue]] = {}
_aux_lock = asyncio.Lock()


async def subscribe_channel(channel: str, bot_id: str) -> asyncio.Queue:
    """Create a subscriber queue for a named auxiliary channel."""
    async with _aux_lock:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        _aux_subscriptions.setdefault((channel, bot_id), []).append(q)
    return q


async def unsubscribe_channel(channel: str, bot_id: str, q: asyncio.Queue) -> None:
    async with _aux_lock:
        key = (channel, bot_id)
        subs = _aux_subscriptions.get(key, [])
        try:
            subs.remove(q)
        except ValueError:
            pass
        if not subs:
            _aux_subscriptions.pop(key, None)


async def push_channel(channel: str, bot_id: str, payload: dict) -> None:
    """Push a payload to all subscribers of (channel, bot_id)."""
    async with _aux_lock:
        subs = list(_aux_subscriptions.get((channel, bot_id), []))
    for q in subs:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("SSE aux queue full for %s/%s — dropping payload", channel, bot_id)


async def close_channel(channel: str, bot_id: str) -> None:
    """Send a sentinel to all subscribers of an auxiliary channel."""
    await push_channel(channel, bot_id, {"__terminal__": True})
