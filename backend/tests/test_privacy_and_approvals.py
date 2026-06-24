"""Privacy controls and action approval queue tests."""

from __future__ import annotations

import httpx
import pytest


async def _account_id(auth_client: httpx.AsyncClient) -> str:
    resp = await auth_client.get("/api/v1/auth/me")
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_consent_policy_applies_to_created_bot(auth_client: httpx.AsyncClient, monkeypatch):
    from app.api import bots as bots_api
    from app.store import store

    async def _noop_lifecycle(bot_id: str) -> None:
        return None

    monkeypatch.setattr(bots_api.bot_service, "run_bot_lifecycle", _noop_lifecycle)

    resp = await auth_client.put(
        "/api/v1/privacy/consent-policy",
        json={
            "require_consent": True,
            "consent_message": "Recording by Acme Notes. Say 'do not record' to opt out.",
            "opt_out_phrase": "do not record",
        },
    )
    assert resp.status_code == 200, resp.text

    create = await auth_client.post(
        "/api/v1/bot",
        json={
            "meeting_url": "https://zoom.us/j/1234567890",
            "bot_name": "Consent Bot",
        },
    )
    assert create.status_code in (200, 201), create.text
    bot = await store.get_bot(create.json()["id"])
    assert bot is not None
    assert bot.consent_enabled is True
    assert bot.consent_message == "Recording by Acme Notes. Say 'do not record' to opt out."
    assert bot.consent_opt_out_phrase == "do not record"


@pytest.mark.asyncio
async def test_deletion_request_owner_can_complete_erasure(
    client: httpx.AsyncClient,
    auth_client: httpx.AsyncClient,
):
    from app.store import BotSession, store

    account_id = await _account_id(auth_client)
    bot = BotSession(
        id="bot-delete-request",
        meeting_url="https://zoom.us/j/1234567890",
        meeting_platform="zoom",
        bot_name="Deletion Bot",
        status="done",
        account_id=account_id,
        transcript=[{"speaker": "Alex", "text": "delete me", "timestamp": 0}],
        analysis={"summary": "Sensitive summary", "action_items": []},
        chapters=[{"title": "Intro"}],
        speaker_stats=[{"speaker": "Alex"}],
    )
    await store.create_bot(bot)

    public_resp = await client.post(
        "/api/v1/privacy/deletion-requests",
        json={
            "bot_id": bot.id,
            "requester_email": "alex@example.com",
            "participant_name": "Alex",
            "reason": "Please remove my data.",
        },
    )
    assert public_resp.status_code == 202, public_resp.text
    request_id = public_resp.json()["id"]
    assert set(public_resp.json()) == {"id", "status", "created_at"}

    list_resp = await auth_client.get("/api/v1/privacy/deletion-requests")
    assert list_resp.status_code == 200, list_resp.text
    assert [row["id"] for row in list_resp.json()] == [request_id]

    complete = await auth_client.patch(
        f"/api/v1/privacy/deletion-requests/{request_id}",
        json={
            "status": "completed",
            "resolution_note": "Erased meeting content.",
            "erase_meeting_data": True,
        },
    )
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "completed"

    erased = await store.get_bot(bot.id)
    assert erased is not None
    assert erased.transcript == []
    assert erased.analysis is None
    assert erased.chapters == []
    assert erased.speaker_stats == []


@pytest.mark.asyncio
async def test_approval_required_linear_integration_queues_and_dispatches(
    auth_client: httpx.AsyncClient,
    monkeypatch,
):
    account_id = await _account_id(auth_client)

    integ = await auth_client.post(
        "/api/v1/integrations",
        json={
            "type": "linear",
            "name": "Product tasks",
            "config": {
                "api_key": "lin_api_test",
                "team_id": "team_test",
                "approval_required": True,
            },
        },
    )
    assert integ.status_code == 201, integ.text

    from app.api.action_items import upsert_action_items
    from app.services.integration_service import dispatch_integrations

    bot_id = "bot-approval"
    await upsert_action_items(
        account_id,
        bot_id,
        [{"task": "Send the proposal", "assignee": "Alex", "due_date": "2026-06-30"}],
    )
    await dispatch_integrations(
        account_id,
        {
            "bot_id": bot_id,
            "meeting_url": "https://zoom.us/j/1234567890",
            "meeting_platform": "zoom",
            "analysis": {
                "summary": "Sales call summary",
                "action_items": [{"task": "Send the proposal", "assignee": "Alex"}],
            },
        },
    )

    approvals = await auth_client.get("/api/v1/action-items/approvals")
    assert approvals.status_code == 200, approvals.text
    rows = approvals.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["payload"]["task"] == "Send the proposal"

    calls = []

    async def _fake_linear(api_key: str, team_id: str, bot_data: dict) -> bool:
        calls.append((api_key, team_id, bot_data))
        return True

    monkeypatch.setattr("app.services.integration_service._post_to_linear", _fake_linear)

    approved = await auth_client.post(f"/api/v1/action-items/approvals/{rows[0]['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "sent"
    assert calls
    assert calls[0][0] == "lin_api_test"
    assert calls[0][2]["analysis"]["action_items"][0]["task"] == "Send the proposal"


@pytest.mark.asyncio
async def test_trust_page_is_public(client: httpx.AsyncClient):
    resp = await client.get("/trust")
    assert resp.status_code == 200
    assert "Trust &amp; Security" in resp.text or "Trust & Security" in resp.text
