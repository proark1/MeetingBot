"""Tests for short-lived WebSocket ticket issuance."""

import httpx
import pytest


@pytest.mark.asyncio
async def test_ws_ticket_is_single_use_for_authenticated_account(auth_client: httpx.AsyncClient):
    """A ticket should resolve to the authenticated account and then disappear."""
    from app.api.ws import _consume_ws_ticket, _ws_tickets

    _ws_tickets.clear()
    me = await auth_client.get("/api/v1/auth/me")
    assert me.status_code == 200

    resp = await auth_client.post("/api/v1/ws/ticket")
    assert resp.status_code == 200
    ticket = resp.json()["ticket"]

    supplied, valid, account_id = _consume_ws_ticket(ticket)
    assert supplied is True
    assert valid is True
    assert account_id == me.json()["id"]

    supplied, valid, account_id = _consume_ws_ticket(ticket)
    assert supplied is True
    assert valid is False
    assert account_id is None


@pytest.mark.asyncio
async def test_ws_ticket_supports_open_dev_mode(client: httpx.AsyncClient):
    """Valid open-dev tickets carry no account but should still be valid."""
    from app.api.ws import _consume_ws_ticket, _ws_tickets

    _ws_tickets.clear()
    resp = await client.post("/api/v1/ws/ticket")
    assert resp.status_code == 200

    supplied, valid, account_id = _consume_ws_ticket(resp.json()["ticket"])
    assert supplied is True
    assert valid is True
    assert account_id is None
