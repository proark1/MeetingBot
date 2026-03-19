"""WebSocket connection manager — broadcasts bot lifecycle events to connected clients.

Each connection is authenticated via a Bearer token passed as the `token` query
parameter.  Events are filtered so each client only receives events for its own
bots (superadmin / unauthenticated dev-mode connections receive all events).
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()


async def _resolve_ws_account(token: Optional[str]) -> Optional[str]:
    """Validate a WS token and return the account_id (or None for open/superadmin)."""
    if not token:
        # No token — allowed only when API_KEY auth is disabled (dev mode)
        from app.config import settings
        if settings.API_KEY:
            return None  # will be rejected in websocket_endpoint
        return None  # dev mode: open access, no filtering

    from app.config import settings

    # Legacy superadmin API_KEY bypass
    if settings.API_KEY and token == settings.API_KEY:
        return "__superadmin__"

    # JWT (web UI sessions)
    if token.startswith("eyJ"):
        try:
            from jose import jwt, JWTError
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
            return payload.get("sub") or None
        except Exception:
            return None  # invalid JWT → reject below

    # Per-user API key (sk_live_...)
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import ApiKey
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
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
        - Connections with account_id="__superadmin__" receive everything.
        - Connections with a specific account_id only receive events whose
          ``data["account_id"]`` matches — or events that carry no account_id.
        """
        if not self._connections:
            return
        message = json.dumps({"event": event, "data": data})

        async def _send(ws: WebSocket, conn_account: Optional[str]) -> WebSocket | None:
            # Decide whether this connection should receive this event
            if conn_account not in (None, "__superadmin__"):
                # Per-user connection: only deliver events that belong to the same account
                event_account = data.get("account_id") or account_id
                if event_account and event_account != conn_account:
                    return None  # skip — not this user's event
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
async def websocket_endpoint(websocket: WebSocket, token: Optional[str] = None) -> None:
    from app.config import settings

    account_id = await _resolve_ws_account(token)

    if account_id == "__db_error__":
        await websocket.close(code=4503, reason="Service temporarily unavailable")
        return

    # Reject unauthenticated connections when auth is required
    if account_id is None and token is not None:
        # Token was provided but invalid
        await websocket.close(code=4003, reason="Invalid token")
        return
    if account_id is None and settings.API_KEY:
        # No token but auth is required
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
