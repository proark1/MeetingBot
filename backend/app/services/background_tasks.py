"""Tracked-task helper.

Plain ``asyncio.create_task(coro)`` does not keep a reference to the task,
so the loop's only strong ref lives inside the running task itself. CPython
explicitly warns that such tasks can be garbage-collected mid-await — which
silently swallows exceptions and drops in-flight work (e.g. audit log entries
or a per-bot webhook fire).

Use ``tracked_task(coro)`` for any fire-and-forget work that needs to actually
finish: it stores the task in a module-level set until completion and removes
it via ``add_done_callback``. Tasks are also cancelled+awaited on shutdown via
``cancel_all_tracked_tasks``, used by the lifespan handler in main.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def tracked_task(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Schedule ``coro`` and keep a strong reference until it finishes."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def cancel_all_tracked_tasks() -> None:
    """Cancel every outstanding tracked task and await their completion.

    Called on app shutdown to avoid leaking partially-flushed audit logs or
    webhook attempts into the void.
    """
    if not _tasks:
        return
    logger.info("Cancelling %d tracked background task(s)…", len(_tasks))
    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*list(_tasks), return_exceptions=True)
