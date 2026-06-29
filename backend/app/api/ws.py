"""WebSocket connection manager — broadcasts bot lifecycle events to connected clients.

Each connection is authenticated with a short-lived ticket from
``POST /api/v1/ws/ticket``.  Legacy ``?token=`` auth is still accepted for
backwards compatibility, but clients should avoid placing long-lived API keys
or JWTs in WebSocket URLs.
"""

import asyncio
import hmac
import json
import logging
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.deps import SUPERADMIN_ACCOUNT_ID, get_current_account_id

logger = logging.getLogger(__name__)
router = APIRouter()
_WS_TICKET_TTL_S = 60
_WS_TICKET_MAX = 5000
_ws_tickets: dict[str, tuple[Optional[str], float]] = {}


def _prune_ws_tickets() -> None:
    now = time.time()
    expired = [t for t, (_, exp) in _ws_tickets.items() if exp <= now]
    for ticket in expired:
        _ws_tickets.pop(ticket, None)
    while len(_ws_tickets) > _WS_TICKET_MAX:
        _ws_tickets.pop(next(iter(_ws_tickets)), None)


@router.post("/ws/ticket")
async def create_ws_ticket(
    request: Request,
    account_id: Optional[str] = Depends(get_current_account_id),
):
    """Issue a single-use, short-lived WebSocket ticket.

    Browser clients should fetch this endpoint with their normal cookie/Bearer
    auth, then connect to ``/api/v1/ws?ticket=...``.  The ticket is intentionally
    short-lived so URL logs never contain long-lived credentials.
    """
    from app.config import settings
    from app import deps as _deps

    if account_id is None and (settings.API_KEY or _deps.require_bearer_in_dev_mode):
        raise HTTPException(status_code=401, detail="Authentication required")
    _prune_ws_tickets()
    ticket = secrets.token_urlsafe(32)
    _ws_tickets[ticket] = (account_id, time.time() + _WS_TICKET_TTL_S)
    return {"ticket": ticket, "expires_in": _WS_TICKET_TTL_S}


def _consume_ws_ticket(ticket: Optional[str]) -> tuple[bool, bool, Optional[str]]:
    if not ticket:
        return False, False, None
    _prune_ws_tickets()
    row = _ws_tickets.pop(ticket, None)
    if row is None:
        return True, False, None
    account_id, expires_at = row
    if expires_at <= time.time():
        return True, False, None
    return True, True, account_id


async def _resolve_ws_account(token: Optional[str]) -> Optional[str]:
    """Validate a WS token and return the account_id (or None for open/superadmin)."""
    if not token:
        # No token — only allowed in true dev mode (no API_KEY, no real accounts).
        # When real accounts exist, the same lockdown that protects HTTP applies
        # here so unauthenticated WS clients can't subscribe to all events.
        from app.config import settings
        from app import deps as _deps
        if settings.API_KEY:
            return None  # will be rejected in websocket_endpoint
        if _deps.require_bearer_in_dev_mode:
            return None  # rejected below
        return None  # dev mode: open access, no filtering

    from app.config import settings

    # Legacy superadmin API_KEY bypass
    if settings.API_KEY and hmac.compare_digest(token, settings.API_KEY):
        return SUPERADMIN_ACCOUNT_ID

    # JWT (web UI sessions)
    if token.startswith("eyJ"):
        try:
            from jose import jwt
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
            return payload.get("sub") or None
        except Exception:
            return None  # invalid JWT → reject below

    # Per-user API key (sk_live_...)
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import ApiKey
        from app.services.token_hash import hash_token
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            api_key = None
            if len(token) >= 16:
                # Preferred: peppered-HMAC lookup (round-3 fix #6).
                result = await db.execute(
                    select(ApiKey).where(
                        ApiKey.key_prefix == token[:16],
                        ApiKey.key_hash == hash_token(token),
                        ApiKey.is_active == True,  # noqa: E712
                    )
                )
                api_key = result.scalar_one_or_none()
            if api_key is None:
                # Legacy plaintext fallback for un-migrated rows.
                result = await db.execute(
                    select(ApiKey).where(ApiKey.key == token, ApiKey.is_active == True)  # noqa: E712
                )
                api_key = result.scalar_one_or_none()
            if api_key:
                return api_key.account_id
    except Exception:
        logger.exception("WS token DB lookup failed")
        return "__db_error__"

    return None  # unknown token → reject


class ConnectionManager:
    def __init__(self) -> None:
        # Maps WebSocket → account_id (None = dev-mode open access)
        self._connections: dict[WebSocket, Optional[str]] = {}

    async def connect(self, ws: WebSocket, account_id: Optional[str]) -> None:
        await ws.accept()
        self._connections[ws] = account_id
        logger.debug("WS connected  account=%s  total=%d", account_id, len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.pop(ws, None)
        logger.debug("WS disconnected  total=%d", len(self._connections))

    async def broadcast(self, event: str, data: dict, account_id: Optional[str] = None) -> None:
        """Send an event to all eligible connections.

        Filtering rules:
        - Connections with account_id=None (dev mode) receive everything.
        - Connections with account_id=SUPERADMIN_ACCOUNT_ID receive everything.
        - Connections with a specific account_id only receive events whose
          ``data["account_id"]`` matches — or events that carry no account_id.
        """
        if not self._connections:
            return
        message = json.dumps({"event": event, "data": data})

        async def _send(ws: WebSocket, conn_account: Optional[str]) -> WebSocket | None:
            # Decide whether this connection should receive this event
            if conn_account not in (None, SUPERADMIN_ACCOUNT_ID):
                # Per-user connection: deliver only events provably owned by this
                # account. Fail closed — if the event carries no resolvable
                # account_id we must NOT deliver it (otherwise an unscoped event
                # leaks to every authenticated tenant).
                event_account = data.get("account_id") or account_id
                if not event_account or event_account != conn_account:
                    return None  # skip — not provably this user's event
            try:
                await asyncio.wait_for(ws.send_text(message), timeout=5.0)
                return None
            except Exception as exc:
                logger.debug("WS send failed — dropping connection: %s", exc)
                return ws

        results = await asyncio.gather(
            *(_send(ws, conn_acct) for ws, conn_acct in set(self._connections.items()))
        )
        dead = {ws for ws in results if ws is not None}
        for ws in dead:
            self._connections.pop(ws, None)


# Module-level singleton — imported by bot_service & webhook_service
manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    ticket: Optional[str] = None,
    token: Optional[str] = None,
) -> None:
    """Real-time event stream via WebSocket.

    **Authentication:** Prefer `POST /api/v1/ws/ticket`, then connect with
    `?ticket=<ticket>`. Legacy `?token=<key>` still works for older clients but
    should be avoided because URLs are commonly logged.

    **Message format (JSON):**
    ```json
    {
        "event": "bot.live_transcript",
        "bot_id": "abc123",
        "account_id": "acc_456",
        "data": { ... }
    }
    ```

    **Events broadcast:**
    - `bot.joining`, `bot.in_call`, `bot.call_ended`, `bot.done`, `bot.error`
    - `bot.live_transcript` — real-time transcript entries during the call
    - `bot.live_transcript_translated` — translated entries (when translation_language is set)
    - `bot.keyword_alert` — keyword/phrase detected in live transcript
    - `bot.coaching_tip` — host coaching tip
    - `bot.coaching_alert` — dominant-speaker coaching alert
    - `bot.action_item` — live-extracted action items

    Events are scoped to the authenticated account's bots.
    """
    from app.config import settings

    ticket_supplied, ticket_valid, account_id = _consume_ws_ticket(ticket)
    if ticket_supplied and not ticket_valid:
        await websocket.close(code=4003, reason="Invalid or expired ticket")
        return
    if not ticket_supplied:
        cookie_token = websocket.cookies.get("mb_token")
        account_id = await _resolve_ws_account(token or cookie_token)

    if account_id == "__db_error__":
        await websocket.close(code=4503, reason="Service temporarily unavailable")
        return

    # Reject unauthenticated connections when auth is required
    if account_id is None and (token is not None or websocket.cookies.get("mb_token")):
        # Token was provided but invalid
        await websocket.close(code=4003, reason="Invalid token")
        return
    if account_id is None and settings.API_KEY:
        # No token but auth is required
        await websocket.close(code=4001, reason="Authentication required")
        return

    # Round-2 fix #9: even when API_KEY is unset, require auth once real
    # accounts exist (fail-closed dev mode).
    from app import deps as _deps
    if account_id is None and token is None and _deps.require_bearer_in_dev_mode:
        await websocket.close(code=4001, reason="Authentication required")
        return

    await manager.connect(websocket, account_id)
    try:
        while True:
            text = await websocket.receive_text()
            if text == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
