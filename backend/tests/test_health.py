"""Smoke tests for health and docs endpoints."""

import asyncio
from contextlib import suppress

import pytest
import httpx


@pytest.mark.asyncio
async def test_health_endpoint(client: httpx.AsyncClient):
    """GET /health should return 200."""
    resp = await client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_api_docs_accessible(client: httpx.AsyncClient):
    """GET /api/docs should return 200."""
    resp = await client.get("/api/docs")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_background_supervisor_refreshes_heartbeat_for_live_worker():
    """Long-running background loops should not go stale while still alive."""
    from app import main as app_main

    name = "test_supervised_worker"
    stop = asyncio.Event()

    async def _worker():
        await stop.wait()

    app_main._task_heartbeats.pop(name, None)
    task = asyncio.create_task(
        app_main._run_supervised_background_task(
            name,
            _worker,
            heartbeat_interval_s=0.01,
        )
    )

    try:
        await asyncio.sleep(0.03)
        first = app_main._task_heartbeats[name]
        await asyncio.sleep(0.03)
        second = app_main._task_heartbeats[name]
        assert second > first
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        app_main._task_heartbeats.pop(name, None)
