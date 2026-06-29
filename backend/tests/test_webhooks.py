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
    # DELETE /api/v1/webhook/{id} is declared status_code=204 (REST convention)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_reject_localhost_webhook(auth_client: httpx.AsyncClient):
    """POST /api/v1/webhook with localhost URL should be rejected."""
    resp = await auth_client.post(
        "/api/v1/webhook",
        json={"url": "http://localhost:8080/hook", "events": ["bot.done"]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_webhook_secret_persisted_encrypted_and_restored(auth_client: httpx.AsyncClient):
    """Webhook HMAC secrets should be encrypted in the DB and plaintext in memory."""
    plaintext = "whsec_" + ("super-secret-value-" * 6)
    resp = await auth_client.post(
        "/api/v1/webhook",
        json={
            "url": "https://example.com/encrypted-secret-hook",
            "events": ["bot.done"],
            "secret": plaintext,
        },
    )
    assert resp.status_code in (200, 201)
    wh_id = resp.json()["id"]

    from app.db import AsyncSessionLocal
    from app.models.account import Webhook
    from app.services.secrets_at_rest import decrypt_text
    from app.store import load_persisted_webhooks, store
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Webhook).where(Webhook.id == wh_id))
        row = result.scalar_one()

    assert row.secret != plaintext
    assert decrypt_text(row.secret) == plaintext

    async with store._lock:
        store._webhooks.clear()
    loaded = await load_persisted_webhooks()
    assert loaded >= 1
    restored = await store.get_webhook(wh_id)
    assert restored is not None
    assert restored.secret == plaintext
