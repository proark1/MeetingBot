"""Smoke tests for bot endpoints."""

import pytest
import httpx


@pytest.mark.asyncio
async def test_create_bot(auth_client: httpx.AsyncClient):
    """POST /api/v1/bot should create a bot."""
    resp = await auth_client.post(
        "/api/v1/bot",
        json={
            "meeting_url": "https://zoom.us/j/1234567890",
            "bot_name": "Test Bot",
        },
    )
    assert resp.status_code in (200, 201)
    data = resp.json()
    assert "id" in data
    assert data.get("status") in ("ready", "scheduled", "queued")


@pytest.mark.asyncio
async def test_list_bots(auth_client: httpx.AsyncClient):
    """GET /api/v1/bot should return a list."""
    resp = await auth_client.get("/api/v1/bot")
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data


@pytest.mark.asyncio
async def test_get_bot(auth_client: httpx.AsyncClient):
    """GET /api/v1/bot/{id} should return the bot."""
    create_resp = await auth_client.post(
        "/api/v1/bot",
        json={"meeting_url": "https://zoom.us/j/9999999999", "bot_name": "Get Test"},
    )
    bot_id = create_resp.json()["id"]

    resp = await auth_client.get(f"/api/v1/bot/{bot_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == bot_id


@pytest.mark.asyncio
async def test_get_nonexistent_bot(auth_client: httpx.AsyncClient):
    """GET /api/v1/bot/{id} for non-existent bot should return 404."""
    resp = await auth_client.get("/api/v1/bot/nonexistent-bot-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_bot(auth_client: httpx.AsyncClient):
    """DELETE /api/v1/bot/{id} should cancel the bot."""
    create_resp = await auth_client.post(
        "/api/v1/bot",
        json={"meeting_url": "https://zoom.us/j/1111111111", "bot_name": "Cancel Test"},
    )
    bot_id = create_resp.json()["id"]

    resp = await auth_client.delete(f"/api/v1/bot/{bot_id}")
    assert resp.status_code == 200
