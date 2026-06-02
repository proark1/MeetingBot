"""Tests for the MCP server surface (schema + tool execution).

Regression coverage for the previously-untested MCP tool-execution path. In
particular `create_bot` used to call `run_bot_lifecycle(bot_id, use_real_bot)`
with an extra positional arg the coroutine does not accept, raising TypeError
in a fire-and-forget task whose exception was swallowed — the bot was created
but stayed in "ready" forever and never joined. It also bypassed the
MAX_CONCURRENT_BOTS slot admission used by every other create path.
"""

import asyncio

import httpx
import pytest


@pytest.mark.asyncio
async def test_mcp_schema_lists_tools(auth_client: httpx.AsyncClient):
    """GET /mcp/schema returns the manifest with the full tool catalogue."""
    resp = await auth_client.get("/api/v1/mcp/schema")
    assert resp.status_code == 200
    manifest = resp.json()
    tools = manifest.get("tools", [])
    names = {t["name"] for t in tools}
    # Core tools must be present (catalogue is 16 tools).
    assert {"list_meetings", "create_bot", "cancel_bot", "ask_chat_qa"} <= names
    assert len(tools) == 16


@pytest.mark.asyncio
async def test_mcp_create_bot_starts_lifecycle(auth_client: httpx.AsyncClient):
    """create_bot must actually start the lifecycle, not leave the bot in 'ready'.

    This is the core regression: the tool now routes through the shared
    slot-admission path, so the bot transitions out of 'ready' instead of
    silently crashing in the background.
    """
    resp = await auth_client.post(
        "/api/v1/mcp/call",
        json={
            "tool": "create_bot",
            "arguments": {
                "meeting_url": "https://zoom.us/j/1234509876",
                "bot_name": "MCP Test Bot",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert "error" not in result, result
    bot_id = result.get("bot_id")
    assert bot_id, result
    assert result.get("platform") == "zoom"
    # The lifecycle was admitted synchronously: status is no longer "ready".
    assert result.get("status") != "ready", result
    assert result.get("status") in (
        "scheduled", "queued", "joining", "in_call", "call_ended",
        "transcribing", "done", "error",
    ), result

    # And the bot is readable + still not stuck in "ready" shortly after.
    await asyncio.sleep(0.1)
    get_resp = await auth_client.get(f"/api/v1/bot/{bot_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] != "ready"


@pytest.mark.asyncio
async def test_mcp_unknown_tool_returns_error(auth_client: httpx.AsyncClient):
    """An unknown tool name returns a 400 with a structured detail, not a 500."""
    resp = await auth_client.post(
        "/api/v1/mcp/call",
        json={"tool": "does_not_exist", "arguments": {}},
    )
    assert resp.status_code == 400
    assert "Unknown tool" in resp.json()["detail"]
