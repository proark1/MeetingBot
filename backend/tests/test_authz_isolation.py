"""Cross-account authorization isolation tests.

Per CLAUDE.md, accessing another tenant's resource must return 404 (not 403) to
avoid leaking existence, and unauthenticated callers must never see a tenant's
bot. The pre-existing test_unauthenticated_request accepts 200-or-401, so it
can't catch an isolation regression — these tests are deterministic.
"""

import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import update

from app.db import AsyncSessionLocal
from app.models.account import Account


async def _register(client: httpx.AsyncClient, credits: str = "100") -> str:
    """Register a fresh account, top it up, and return its API key."""
    email = f"iso-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "TestPassword123!", "key_name": "k"},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    api_key = body.get("api_key") or body.get("key") or body.get("access_token", "")
    assert api_key, f"no api key in register response: {body}"
    async with AsyncSessionLocal() as s:
        await s.execute(
            update(Account).where(Account.email == email).values(credits_usd=Decimal(credits))
        )
        await s.commit()
    return api_key


@pytest.mark.asyncio
async def test_cross_account_bot_access_returns_404(client: httpx.AsyncClient):
    key_a = await _register(client)
    key_b = await _register(client)

    # Account A creates a bot.
    created = await client.post(
        "/api/v1/bot",
        json={"meeting_url": "https://zoom.us/j/424242", "bot_name": "A's bot"},
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert created.status_code in (200, 201), created.text
    bot_id = created.json()["id"]

    # A can read its own bot.
    own = await client.get(
        f"/api/v1/bot/{bot_id}", headers={"Authorization": f"Bearer {key_a}"}
    )
    assert own.status_code == 200
    assert own.json()["id"] == bot_id

    # B must NOT — and must get 404 (not 403), to avoid leaking existence.
    other = await client.get(
        f"/api/v1/bot/{bot_id}", headers={"Authorization": f"Bearer {key_b}"}
    )
    assert other.status_code == 404

    # An unauthenticated caller must never see A's bot.
    anon = await client.get(f"/api/v1/bot/{bot_id}")
    assert anon.status_code != 200


@pytest.mark.asyncio
async def test_invalid_bearer_is_rejected(client: httpx.AsyncClient):
    # A syntactically-bogus token must not authenticate as anyone.
    resp = await client.get(
        "/api/v1/bot",
        headers={"Authorization": "Bearer sk_live_obviously-not-a-real-key"},
    )
    assert resp.status_code in (401, 403)
