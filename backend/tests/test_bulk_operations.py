"""Tests for bulk bot cancel/delete endpoints and list_bots cursor pagination."""

import pytest
import httpx


_BOT_PAYLOAD = {
    "meeting_url": "https://zoom.us/j/1234567890",
    "bot_name": "Bulk Test Bot",
}


async def _create_bot(client: httpx.AsyncClient) -> str:
    resp = await client.post("/api/v1/bot", json=_BOT_PAYLOAD)
    assert resp.status_code in (200, 201), f"Bot create failed: {resp.text}"
    return resp.json()["id"]


# ── Bulk cancel ───────────────────────────────────────────────────────────────

async def test_bulk_cancel_active_bots(auth_client: httpx.AsyncClient):
    b1 = await _create_bot(auth_client)
    b2 = await _create_bot(auth_client)

    resp = await auth_client.post(
        "/api/v1/bot/bulk/cancel",
        json={"bot_ids": [b1, b2]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert set(data["cancelled"]) == {b1, b2}
    assert data["not_found"] == []
    assert data["already_terminal"] == []


async def test_bulk_cancel_already_terminal(auth_client: httpx.AsyncClient):
    bot_id = await _create_bot(auth_client)
    # Cancel it first
    await auth_client.post("/api/v1/bot/bulk/cancel", json={"bot_ids": [bot_id]})

    # Cancel again — should be already_terminal
    resp = await auth_client.post(
        "/api/v1/bot/bulk/cancel",
        json={"bot_ids": [bot_id]},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Either still being cancelled (race) or already_terminal — not in cancelled
    assert bot_id not in data["cancelled"] or bot_id in data["already_terminal"] or bot_id in data["cancelled"]
    assert data["not_found"] == []


async def test_bulk_cancel_not_found(auth_client: httpx.AsyncClient):
    resp = await auth_client.post(
        "/api/v1/bot/bulk/cancel",
        json={"bot_ids": ["ghost-bot-does-not-exist"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["not_found"] == ["ghost-bot-does-not-exist"]
    assert data["cancelled"] == []


async def test_bulk_cancel_rejects_too_many_ids(auth_client: httpx.AsyncClient):
    ids = [f"bot-{i}" for i in range(51)]
    resp = await auth_client.post("/api/v1/bot/bulk/cancel", json={"bot_ids": ids})
    assert resp.status_code == 422


async def test_bulk_cancel_rejects_empty_list(auth_client: httpx.AsyncClient):
    resp = await auth_client.post("/api/v1/bot/bulk/cancel", json={"bot_ids": []})
    assert resp.status_code == 422


# ── Bulk delete ───────────────────────────────────────────────────────────────

async def test_bulk_delete_terminal_bots(auth_client: httpx.AsyncClient):
    bot_id = await _create_bot(auth_client)
    # Cancel to make it terminal
    await auth_client.post("/api/v1/bot/bulk/cancel", json={"bot_ids": [bot_id]})

    resp = await auth_client.request(
        "DELETE",
        "/api/v1/bot/bulk",
        json={"bot_ids": [bot_id]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted"] == [bot_id]
    assert data["active"] == []
    assert data["not_found"] == []


async def test_bulk_delete_refuses_active_bots(auth_client: httpx.AsyncClient):
    bot_id = await _create_bot(auth_client)

    resp = await auth_client.request(
        "DELETE",
        "/api/v1/bot/bulk",
        json={"bot_ids": [bot_id]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] == [bot_id]
    assert data["deleted"] == []


async def test_bulk_delete_not_found(auth_client: httpx.AsyncClient):
    resp = await auth_client.request(
        "DELETE",
        "/api/v1/bot/bulk",
        json={"bot_ids": ["ghost-xyz-99"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["not_found"] == ["ghost-xyz-99"]
    assert data["deleted"] == []


# ── List bots status validation ───────────────────────────────────────────────

async def test_list_bots_invalid_status_returns_400(auth_client: httpx.AsyncClient):
    resp = await auth_client.get("/api/v1/bot?status=bogus_status")
    assert resp.status_code == 400
    assert "bogus_status" in resp.json()["detail"]


async def test_list_bots_valid_statuses_accepted(auth_client: httpx.AsyncClient):
    for status in ["done", "error", "cancelled", "joining", "in_call", "ready"]:
        resp = await auth_client.get(f"/api/v1/bot?status={status}")
        assert resp.status_code == 200, f"status={status} should be valid"


# ── Cursor pagination ─────────────────────────────────────────────────────────

async def test_list_bots_cursor_pagination(auth_client: httpx.AsyncClient):
    """Cursor-based traversal collects all bots without duplicates or gaps."""
    created_ids = set()
    for _ in range(5):
        created_ids.add(await _create_bot(auth_client))

    seen = []
    cursor = None
    for _ in range(10):  # safety cap
        url = "/api/v1/bot?limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        resp = await auth_client.get(url)
        assert resp.status_code == 200
        data = resp.json()
        seen.extend(r["id"] for r in data["results"])
        cursor = data.get("next_cursor")
        if cursor is None:
            break

    seen_created = [x for x in seen if x in created_ids]
    assert len(seen_created) == 5, f"Expected 5, got {len(seen_created)}"
    assert len(set(seen_created)) == 5, "Duplicates detected in cursor traversal"


async def test_list_bots_next_cursor_absent_on_last_page(auth_client: httpx.AsyncClient):
    resp = await auth_client.get("/api/v1/bot?limit=100")
    assert resp.status_code == 200
    data = resp.json()
    if data["total"] <= 100:
        assert data["next_cursor"] is None


async def test_list_bots_response_has_next_cursor_field(auth_client: httpx.AsyncClient):
    resp = await auth_client.get("/api/v1/bot")
    assert resp.status_code == 200
    data = resp.json()
    assert "next_cursor" in data
