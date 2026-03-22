"""Smoke tests for webhook endpoints."""

import pytest
import httpx


@pytest.mark.asyncio
async def test_create_webhook(auth_client: httpx.AsyncClient):
    """POST /api/v1/webhook should register a webhook."""
    resp = await auth_client.post(
        "/api/v1/webhook",
        json={
            "url": "https://example.com/webhook",
            "events": ["bot.done", "bot.error"],
        },
    )
    assert resp.status_code in (200, 201)
    data = resp.json()
    assert "id" in data
    assert data["url"] == "https://example.com/webhook"


@pytest.mark.asyncio
async def test_list_webhooks(auth_client: httpx.AsyncClient):
    """GET /api/v1/webhook should return a list."""
    resp = await auth_client.get("/api/v1/webhook")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_webhook(auth_client: httpx.AsyncClient):
    """GET /api/v1/webhook/{id} should return the webhook."""
    create_resp = await auth_client.post(
        "/api/v1/webhook",
        json={"url": "https://example.com/hook2", "events": ["bot.done"]},
    )
    wh_id = create_resp.json()["id"]

    resp = await auth_client.get(f"/api/v1/webhook/{wh_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == wh_id


@pytest.mark.asyncio
async def test_delete_webhook(auth_client: httpx.AsyncClient):
    """DELETE /api/v1/webhook/{id} should remove the webhook."""
    create_resp = await auth_client.post(
        "/api/v1/webhook",
        json={"url": "https://example.com/hook3", "events": ["bot.done"]},
    )
    wh_id = create_resp.json()["id"]

    resp = await auth_client.delete(f"/api/v1/webhook/{wh_id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_reject_localhost_webhook(auth_client: httpx.AsyncClient):
    """POST /api/v1/webhook with localhost URL should be rejected."""
    resp = await auth_client.post(
        "/api/v1/webhook",
        json={"url": "http://localhost:8080/hook", "events": ["bot.done"]},
    )
    assert resp.status_code == 400
