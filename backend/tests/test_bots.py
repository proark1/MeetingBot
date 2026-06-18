"""Smoke tests for bot endpoints."""

import pytest
import httpx


@pytest.mark.asyncio
async def test_validate_known_unsupported_platform_is_demo_only(auth_client: httpx.AsyncClient):
    """Validation distinguishes recognized URLs from real browser support."""
    resp = await auth_client.post(
        "/api/v1/bot/validate-meeting-url",
        json={"meeting_url": "https://acme.webex.com/meet/alice"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["platform"] == "webex"
    assert data["supported"] is False
    assert "Demo mode" in data["message"]


@pytest.mark.asyncio
async def test_create_known_unsupported_platform_requires_demo_opt_in(auth_client: httpx.AsyncClient):
    """POST /bot should not silently create a demo bot for a real recording request."""
    resp = await auth_client.post(
        "/api/v1/bot",
        json={"meeting_url": "https://acme.webex.com/meet/alice", "bot_name": "Webex Test"},
    )
    assert resp.status_code == 422
    assert "allow_demo_mode=true" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_known_unsupported_platform_allows_explicit_demo(
    auth_client: httpx.AsyncClient,
    monkeypatch,
):
    """Demo mode remains available, but only when explicitly requested."""
    from app.api import bots as bots_api

    async def _noop_lifecycle(bot_id: str) -> None:
        return None

    monkeypatch.setattr(bots_api.bot_service, "run_bot_lifecycle", _noop_lifecycle)

    resp = await auth_client.post(
        "/api/v1/bot",
        json={
            "meeting_url": "https://acme.webex.com/meet/alice",
            "bot_name": "Webex Demo",
            "allow_demo_mode": True,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    assert data["meeting_platform"] == "webex"


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
    # The bot starts transitioning immediately after create; any early-lifecycle
    # status is acceptable here (status strings per CLAUDE.md).
    assert data.get("status") in ("ready", "scheduled", "queued", "joining")


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
    # DELETE /api/v1/bot/{id} is declared status_code=204 (REST convention)
    assert resp.status_code == 204
