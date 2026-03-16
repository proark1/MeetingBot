"""Global webhook registration API.

Webhooks registered here receive events for ALL bots.
For per-bot webhooks, pass `webhook_url` when creating a bot.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException

from app.schemas.webhook import WebhookCreate, WebhookResponse
from app.store import store

router = APIRouter(prefix="/webhook", tags=["Webhooks"])

_PRIVATE_NETS = [ipaddress.ip_network(n) for n in [
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
    "192.168.0.0/16", "169.254.0.0/16", "::1/128", "fc00::/7",
]]


def _is_private_ip(addr) -> bool:
    return addr.is_private or addr.is_loopback or addr.is_link_local or any(addr in net for net in _PRIVATE_NETS)


async def _block_ssrf(url: str) -> None:
    host = urlparse(url).hostname or ""
    if not host or host.lower() == "localhost":
        raise HTTPException(status_code=400, detail="Webhook URL must not target localhost")
    try:
        addr = ipaddress.ip_address(host)
        if _is_private_ip(addr):
            raise HTTPException(status_code=400, detail="Webhook URL must not target a private address")
        return
    except ValueError:
        pass
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        for *_, sockaddr in infos:
            try:
                addr = ipaddress.ip_address(sockaddr[0])
                if _is_private_ip(addr):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Webhook URL resolves to a private address ({sockaddr[0]})",
                    )
            except ValueError:
                pass
    except socket.gaierror:
        pass


def _to_response(wh) -> WebhookResponse:
    return WebhookResponse(
        id=wh.id,
        url=wh.url,
        events=wh.events,
        is_active=wh.is_active,
        created_at=wh.created_at,
        delivery_attempts=wh.delivery_attempts,
        last_delivery_at=wh.last_delivery_at,
        last_delivery_status=wh.last_delivery_status,
        consecutive_failures=wh.consecutive_failures,
    )


@router.post("", response_model=WebhookResponse, status_code=201)
async def create_webhook(payload: WebhookCreate):
    """Register a global webhook.

    The webhook will receive events for all bots:
    - `bot.joining`, `bot.in_call`, `bot.call_ended`
    - `bot.transcript_ready`, `bot.analysis_ready`
    - `bot.done`, `bot.error`, `bot.cancelled`

    Use `events: ["*"]` to receive all events (default), or list specific ones.

    For per-bot webhooks (results from a single bot), pass `webhook_url` when
    creating the bot instead.
    """
    await _block_ssrf(payload.url)
    wh = await store.new_webhook(url=payload.url, events=payload.events, secret=payload.secret)
    return _to_response(wh)


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks():
    """List all registered global webhooks."""
    return [_to_response(wh) for wh in store.list_webhooks()]


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(webhook_id: str):
    wh = store.get_webhook(webhook_id)
    if wh is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")
    return _to_response(wh)


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: str):
    if not await store.delete_webhook(webhook_id):
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")


@router.post("/{webhook_id}/test")
async def test_webhook(webhook_id: str):
    """Send a test event to this webhook endpoint."""
    wh = store.get_webhook(webhook_id)
    if wh is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")
    await _block_ssrf(wh.url)

    from app.services.webhook_service import _get_client, _deliver_with_retry, _build_body, _sign
    body = _build_body("bot.test", {"message": "Test delivery from MeetingBot"})
    headers = {"Content-Type": "application/json", "User-Agent": "MeetingBot/1.0"}
    if wh.secret:
        headers["X-MeetingBot-Signature"] = _sign(body, wh.secret)

    status_code = await _deliver_with_retry(_get_client(), wh.url, body, headers)
    if status_code is None:
        raise HTTPException(status_code=502, detail="Test delivery failed — endpoint unreachable or returned 5xx")
    return {"status_code": status_code, "url": wh.url}
