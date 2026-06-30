"""Dashboard routes must preserve the same security guarantees as API routes."""

from datetime import datetime, timezone
from decimal import Decimal
import uuid

import httpx
import pytest
from sqlalchemy import select, update

from app.db import AsyncSessionLocal
from app.models.account import Account, CalendarFeed, Integration


async def _register_account(
    client: httpx.AsyncClient,
    *,
    plan: str = "free",
    credits: str = "100",
) -> dict:
    email = f"dash-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "TestPassword123!", "key_name": "dashboard"},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    account_id = body["account_id"]
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(plan=plan, credits_usd=Decimal(credits))
        )
        await db.commit()

    from app.api.auth import _create_jwt

    return {
        "id": account_id,
        "email": email,
        "api_key": body["api_key"],
        "jwt": _create_jwt(account_id),
    }


def _dashboard_headers(jwt_token: str) -> dict:
    return {
        "Cookie": f"mb_token={jwt_token}",
        "Origin": "http://test",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


@pytest.mark.asyncio
async def test_dashboard_webhook_is_tenant_scoped(client: httpx.AsyncClient):
    from app.store import store

    async with store._lock:
        store._webhooks.clear()

    account_a = await _register_account(client)
    account_b = await _register_account(client)

    resp = await client.post(
        "/dashboard/webhook",
        json={"url": "https://example.com/dashboard-webhook", "events": ["bot.done"]},
        headers=_dashboard_headers(account_a["jwt"]),
    )
    assert resp.status_code == 201, resp.text
    webhook_id = resp.json()["id"]
    assert resp.json()["account_id"] == account_a["id"]

    own_webhooks = await store.active_webhooks(account_id=account_a["id"])
    other_webhooks = await store.active_webhooks(account_id=account_b["id"])
    assert webhook_id in {wh.id for wh in own_webhooks}
    assert webhook_id not in {wh.id for wh in other_webhooks}


@pytest.mark.asyncio
async def test_dashboard_webhook_uses_api_event_validation(client: httpx.AsyncClient):
    account = await _register_account(client)

    resp = await client.post(
        "/dashboard/webhook",
        json={
            "url": "https://example.com/dashboard-webhook-invalid",
            "events": ["bot.not_real_event"],
        },
        headers=_dashboard_headers(account["jwt"]),
    )
    assert resp.status_code == 422
    assert "Unknown event" in resp.text


@pytest.mark.asyncio
async def test_dashboard_webhook_accepts_live_events_emitted_by_services(client: httpx.AsyncClient):
    account = await _register_account(client)

    resp = await client.post(
        "/dashboard/webhook",
        json={
            "url": "https://example.com/dashboard-webhook-live",
            "events": ["bot.live_action_items", "bot.live_keyword_alert"],
        },
        headers=_dashboard_headers(account["jwt"]),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["events"] == ["bot.live_action_items", "bot.live_keyword_alert"]


@pytest.mark.asyncio
async def test_dashboard_integration_config_is_encrypted(client: httpx.AsyncClient):
    account = await _register_account(client, plan="starter")

    resp = await client.post(
        "/dashboard/integrations/add",
        json={
            "type": "notion",
            "name": "Notes DB",
            "notion_token": "secret_dashboard_token",
            "notion_database_id": "abc123",
        },
        headers=_dashboard_headers(account["jwt"]),
    )
    assert resp.status_code == 201, resp.text
    integration_id = resp.json()["id"]

    from app.services.secrets_at_rest import decrypt_json

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Integration).where(Integration.id == integration_id))
        row = result.scalar_one()

    assert "secret_dashboard_token" not in row.config
    assert decrypt_json(row.config) == {
        "api_token": "secret_dashboard_token",
        "database_id": "abc123",
    }


@pytest.mark.asyncio
async def test_dashboard_calendar_uses_feature_gate_and_ssrf_guard(client: httpx.AsyncClient):
    account = await _register_account(client, plan="free")

    gated = await client.post(
        "/dashboard/calendar/add",
        json={"name": "Work", "ical_url": "https://example.com/calendar.ics"},
        headers=_dashboard_headers(account["jwt"]),
    )
    assert gated.status_code == 403
    assert "calendar_auto_join" in gated.text

    async with AsyncSessionLocal() as db:
        await db.execute(update(Account).where(Account.id == account["id"]).values(plan="starter"))
        await db.commit()

    blocked = await client.post(
        "/dashboard/calendar/add",
        json={"name": "Local", "ical_url": "http://localhost/calendar.ics"},
        headers=_dashboard_headers(account["jwt"]),
    )
    assert blocked.status_code == 400

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CalendarFeed).where(CalendarFeed.account_id == account["id"]))
        assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_dashboard_wallet_requires_ownership_signature(client: httpx.AsyncClient):
    account = await _register_account(client)
    address = "0x1111111111111111111111111111111111111111"

    resp = await client.post(
        "/dashboard/wallet",
        json={"wallet_address": address},
        headers=_dashboard_headers(account["jwt"]),
    )
    assert resp.status_code == 400
    assert "signature" in resp.text.lower()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Account).where(Account.id == account["id"]))
        row = result.scalar_one()
    assert row.wallet_address is None


@pytest.mark.asyncio
async def test_dashboard_share_forwards_expiry_to_api(client: httpx.AsyncClient):
    account = await _register_account(client)

    from app.store import BotSession, store

    bot_id = f"bot-{uuid.uuid4().hex[:8]}"
    await store.create_bot(
        BotSession(
            id=bot_id,
            meeting_url="https://zoom.us/j/424242",
            meeting_platform="zoom",
            bot_name="Meeting Notetaker",
            status="done",
            account_id=account["id"],
        )
    )

    resp = await client.post(
        f"/dashboard/bot/{bot_id}/share",
        json={"expires_in_hours": 1},
        headers=_dashboard_headers(account["jwt"]),
    )
    assert resp.status_code == 200, resp.text

    updated = await store.get_bot(bot_id)
    assert updated is not None
    assert updated.share_token_hash
    assert updated.share_token_expires_at is not None
    remaining = updated.share_token_expires_at - datetime.now(timezone.utc)
    assert 0 < remaining.total_seconds() <= 3600


@pytest.mark.asyncio
async def test_create_bot_validates_body_sub_user_id(client: httpx.AsyncClient):
    account = await _register_account(client)

    resp = await client.post(
        "/api/v1/bot",
        json={"meeting_url": "https://zoom.us/j/424242", "sub_user_id": "bad value"},
        headers={"Authorization": f"Bearer {account['api_key']}"},
    )
    assert resp.status_code == 400
    assert "X-Sub-User" in resp.text
