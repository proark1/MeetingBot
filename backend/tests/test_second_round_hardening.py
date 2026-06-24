"""Second-round audit hardening regression tests."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _account(db, account_id: str, email: str):
    from app.models.account import Account

    row = Account(
        id=account_id,
        email=email,
        hashed_password="x",
        credits_usd=Decimal("100"),
    )
    db.add(row)
    return row


@pytest.mark.asyncio
async def test_ui_cookie_forms_require_same_origin(client: httpx.AsyncClient):
    resp = await client.post(
        "/login",
        data={"email": "nobody@example.com", "password": "bad"},
    )
    assert resp.status_code == 403
    assert "Cross-site" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_api_routes_are_not_blocked_by_ui_csrf(client: httpx.AsyncClient):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"csrf-{uuid.uuid4().hex[:8]}@test.com",
            "password": "TestPassword123!",
            "key_name": "csrf",
        },
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.mark.asyncio
async def test_inactive_workspace_members_do_not_see_workspace_bots(app):
    from app.api.bots import _check_workspace_role, _workspace_ids_for_account
    from app.db import AsyncSessionLocal
    from app.models.account import Workspace, WorkspaceMember
    from app.store import BotSession

    owner_id = _id("acct-owner")
    member_id = _id("acct-member")
    ws_id = _id("ws")

    async with AsyncSessionLocal() as db:
        await _account(db, owner_id, f"{owner_id}@test.com")
        await _account(db, member_id, f"{member_id}@test.com")
        db.add(Workspace(id=ws_id, name="Deleted", slug=ws_id, owner_account_id=owner_id, is_active=False))
        db.add(WorkspaceMember(workspace_id=ws_id, account_id=member_id, role="member"))
        await db.commit()

    assert ws_id not in await _workspace_ids_for_account(member_id)

    bot = BotSession(
        id=_id("bot"),
        meeting_url="https://zoom.us/j/1234567890",
        meeting_platform="zoom",
        bot_name="Workspace bot",
        status="done",
        account_id=owner_id,
        workspace_id=ws_id,
    )
    with pytest.raises(HTTPException) as exc:
        await _check_workspace_role(bot, member_id, "viewer")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_bot_workspace_create_requires_member_role(app):
    from app.api.bots import _validate_workspace_for_create
    from app.db import AsyncSessionLocal
    from app.models.account import Workspace, WorkspaceMember

    owner_id = _id("acct-owner")
    viewer_id = _id("acct-viewer")
    member_id = _id("acct-member")
    ws_id = _id("ws")

    async with AsyncSessionLocal() as db:
        await _account(db, owner_id, f"{owner_id}@test.com")
        await _account(db, viewer_id, f"{viewer_id}@test.com")
        await _account(db, member_id, f"{member_id}@test.com")
        db.add(Workspace(id=ws_id, name="Active", slug=ws_id, owner_account_id=owner_id, is_active=True))
        db.add(WorkspaceMember(workspace_id=ws_id, account_id=viewer_id, role="viewer"))
        db.add(WorkspaceMember(workspace_id=ws_id, account_id=member_id, role="member"))
        await db.commit()

    with pytest.raises(HTTPException) as exc:
        await _validate_workspace_for_create(ws_id, viewer_id)
    assert exc.value.status_code == 403

    await _validate_workspace_for_create(ws_id, member_id)
    await _validate_workspace_for_create(ws_id, owner_id)


@pytest.mark.asyncio
async def test_mcp_reads_workspace_visible_terminal_snapshots(app):
    from app.db import AsyncSessionLocal
    from app.models.account import Workspace, WorkspaceMember
    from app.services.mcp_service import execute_tool
    from app.store import BotSession, store

    owner_id = _id("acct-owner")
    member_id = _id("acct-member")
    ws_id = _id("ws")
    bot_id = _id("bot")

    async with AsyncSessionLocal() as db:
        await _account(db, owner_id, f"{owner_id}@test.com")
        await _account(db, member_id, f"{member_id}@test.com")
        db.add(Workspace(id=ws_id, name="Shared", slug=ws_id, owner_account_id=owner_id, is_active=True))
        db.add(WorkspaceMember(workspace_id=ws_id, account_id=member_id, role="viewer"))
        await db.commit()

    bot = BotSession(
        id=bot_id,
        meeting_url="https://meet.google.com/abc-defg-hij",
        meeting_platform="google_meet",
        bot_name="Shared history",
        status="ready",
        account_id=owner_id,
        workspace_id=ws_id,
        transcript=[{"speaker": "Alice", "text": "Shared snapshot text", "timestamp": 1.0}],
        analysis={"summary": "Shared summary", "action_items": [{"task": "Follow up", "assignee": "Alice"}]},
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
    )
    await store.create_bot(bot)
    await store.mark_terminal(bot_id, "done")
    await store.delete_bot(bot_id)

    listed = await execute_tool("list_meetings", {"limit": 10}, member_id)
    assert any(row["id"] == bot_id for row in listed["meetings"])

    detail = await execute_tool("get_meeting", {"bot_id": bot_id}, member_id)
    assert detail["id"] == bot_id
    assert detail["analysis"]["summary"] == "Shared summary"

    search = await execute_tool("search_meetings", {"query": "snapshot"}, member_id)
    assert search["total"] == 1
    assert search["results"][0]["bot_id"] == bot_id


@pytest.mark.asyncio
async def test_repeated_oauth_login_does_not_mint_keys_for_hashed_account(app):
    from app.api.auth import api_key_storage_fields
    from app.db import AsyncSessionLocal
    from app.models.account import ApiKey, OAuthAccount
    from app.services.oauth_service import upsert_oauth_account

    account_id = _id("acct-oauth")
    async with AsyncSessionLocal() as db:
        await _account(db, account_id, "oauth-repeat@test.com")
        db.add(ApiKey(
            account_id=account_id,
            name="Existing hashed key",
            **api_key_storage_fields("sk_live_existing_hashed_key_for_repeat_test"),
        ))
        db.add(OAuthAccount(
            account_id=account_id,
            provider="google",
            provider_user_id="google-user-1",
            email="oauth-repeat@test.com",
            access_token="old",
            refresh_token="old-refresh",
        ))
        await db.commit()

    returned_account_id, api_key = await upsert_oauth_account(
        "google",
        {"access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 3600},
        {"sub": "google-user-1", "email": "oauth-repeat@test.com", "email_verified": True},
    )
    assert returned_account_id == account_id
    assert api_key == ""

    async with AsyncSessionLocal() as db:
        count = await db.scalar(select(func.count(ApiKey.id)).where(ApiKey.account_id == account_id))
    assert count == 1
