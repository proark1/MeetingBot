"""WebSocket connection manager — broadcasts bot lifecycle events to all connected clients."""

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        logger.debug("WS connected  total=%d", len(self._connections))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        logger.debug("WS disconnected  total=%d", len(self._connections))

    async def broadcast(self, event: str, data: dict) -> None:
        if not self._connections:
            return
        message = json.dumps({"event": event, "data": data})
        dead: set[WebSocket] = set()
        for ws in set(self._connections):  # snapshot to avoid mutation mid-loop
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        self._connections -= dead


# Module-level singleton — imported by bot_service & webhook_service
manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            text = await websocket.receive_text()
            if text == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
