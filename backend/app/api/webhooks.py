"""Global webhook registration API.

Webhooks registered here receive events for ALL bots.
For per-bot webhooks, pass `webhook_url` when creating a bot.
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request as _Request
from slowapi import Limiter as _Limiter
from slowapi.util import get_remote_address as _get_remote_address

from app.schemas.webhook import WebhookCreate, WebhookResponse
from app.store import store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["Webhooks"])
_limiter = _Limiter(key_func=_get_remote_address)

# All supported webhook event names (wildcard "*" also accepted)
WEBHOOK_EVENTS: list[str] = [
    "bot.joining",
    "bot.in_call",
    "bot.call_ended",
    "bot.transcript_ready",
    "bot.analysis_ready",
    "bot.done",
    "bot.error",
    "bot.cancelled",
    "bot.keyword_alert",
    "bot.live_transcript",
    "bot.live_transcript_translated",
    "bot.live_chat_message",
    "bot.recurring_intel_ready",
    "bot.test",
]

# Private/reserved IP ranges — all traffic to these must be blocked (SSRF prevention).
# Includes cloud-provider metadata endpoints (169.254.169.254 is AWS/GCP/Azure IMDS).
_BLOCKED_NETS = [ipaddress.ip_network(n) for n in [
    "127.0.0.0/8",       # loopback
    "10.0.0.0/8",        # RFC 1918 private
    "172.16.0.0/12",     # RFC 1918 private
    "192.168.0.0/16",    # RFC 1918 private
    "169.254.0.0/16",    # link-local / cloud metadata (AWS IMDS, GCP, Azure)
    "100.64.0.0/10",     # carrier-grade NAT (RFC 6598)
    "192.0.0.0/24",      # IETF protocol assignments
    "198.18.0.0/15",     # benchmark testing
    "198.51.100.0/24",   # TEST-NET-2 (documentation)
    "203.0.113.0/24",    # TEST-NET-3 (documentation)
    "::1/128",           # IPv6 loopback
    "fc00::/7",          # IPv6 unique local
    "fe80::/10",         # IPv6 link-local
]]
_ALLOWED_SCHEMES = {"http", "https"}


def _is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return addr.is_private or addr.is_loopback or addr.is_link_local or any(
        addr in net for net in _BLOCKED_NETS
    )


async def _block_ssrf(url: str) -> None:
    """Reject webhook URLs that target internal/private infrastructure.

    Defends against SSRF by:
    1. Enforcing http/https scheme only
    2. Blocking localhost and private IP literals directly
    3. Resolving hostnames and blocking if any resolved IP is private
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail=f"Webhook URL scheme must be http or https (got {parsed.scheme!r})")
    host = parsed.hostname or ""
    if not host or host.lower() in ("localhost", "0.0.0.0"):
        raise HTTPException(status_code=400, detail="Webhook URL must not target localhost")
    try:
        addr = ipaddress.ip_address(host)
        if _is_blocked_ip(addr):
            raise HTTPException(status_code=400, detail="Webhook URL must not target a private/reserved address")
        return
    except ValueError:
        pass  # not an IP literal — resolve below
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, None),
            timeout=5.0,
        )
        for *_, sockaddr in infos:
            try:
                addr = ipaddress.ip_address(sockaddr[0])
                if _is_blocked_ip(addr):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Webhook URL resolves to a private/reserved address ({sockaddr[0]})",
                    )
            except ValueError:
                pass
    except asyncio.TimeoutError:
        raise HTTPException(status_code=400, detail="Webhook URL DNS resolution timed out")
    except socket.gaierror:
        pass  # hostname unresolvable — let the delivery attempt fail naturally


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
@_limiter.limit("10/minute")
async def create_webhook(payload: WebhookCreate, request: _Request):
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

    import asyncio as _asyncio
    from app.services.audit_log_service import log_event as _audit
    _asyncio.create_task(_audit(
        account_id=None,
        action="webhook.created",
        resource_type="webhook",
        resource_id=wh.id,
        ip_address=request.client.host if request.client else None,
        details={"url": wh.url, "events": wh.events},
    ))

    return _to_response(wh)


@router.get("")
async def list_webhooks():
    """List all registered global webhooks.

    Returns a paginated envelope with `results`, `total`, and `has_more`.
    """
    webhooks = await store.list_webhooks()
    return {
        "results": [_to_response(wh) for wh in webhooks],
        "total": len(webhooks),
        "limit": len(webhooks),
        "offset": 0,
        "has_more": False,
    }


@router.get("/events")
async def list_webhook_events():
    """Return the list of all supported webhook event names.

    Use these values in the `events` array when registering a webhook.
    Pass `["*"]` to subscribe to all events.
    """
    return {"events": WEBHOOK_EVENTS}


# ── Delivery log ───────────────────────────────────────────────────────────────

from fastapi import Request as _Request
from pydantic import BaseModel as _BM
from typing import Optional as _Opt
from datetime import datetime as _dt


class DeliveryResponse(_BM):
    id: str
    webhook_id: str
    bot_id: _Opt[str] = None
    event: str
    status: str
    attempt_number: int
    response_status_code: _Opt[int] = None
    response_body: _Opt[str] = None
    error_message: _Opt[str] = None
    next_retry_at: _Opt[_dt] = None
    delivered_at: _Opt[_dt] = None
    created_at: _dt


@router.get("/deliveries", tags=["Webhooks"])
async def list_all_deliveries(
    limit: int = 50,
    offset: int = 0,
):
    """List the most recent webhook delivery attempts across ALL registered webhooks.

    Useful for the webhook testing playground. Returns a paginated envelope with `results`,
    `total`, `limit`, `offset`, and `has_more`.
    """
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import WebhookDelivery
        from sqlalchemy import select, func
        async with AsyncSessionLocal() as session:
            total_result = await session.execute(select(func.count(WebhookDelivery.id)))
            total = total_result.scalar() or 0
            result = await session.execute(
                select(WebhookDelivery)
                .order_by(WebhookDelivery.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = result.scalars().all()
    except Exception:
        logger.exception("Failed to list webhook deliveries")
        raise HTTPException(status_code=500, detail="Internal server error")

    items = [
        DeliveryResponse(
            id=r.id,
            webhook_id=r.webhook_id,
            bot_id=r.bot_id,
            event=r.event,
            status=r.status,
            attempt_number=r.attempt_number,
            response_status_code=r.response_status_code,
            response_body=r.response_body,
            error_message=r.error_message,
            next_retry_at=r.next_retry_at,
            delivered_at=r.delivered_at,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return {
        "results": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


class WebhookUpdate(_BM):
    """Partial update for a registered webhook."""
    url: _Opt[str] = None
    events: _Opt[list[str]] = None
    is_active: _Opt[bool] = None


@router.patch("/{webhook_id}", response_model=WebhookResponse)
@_limiter.limit("10/minute")
async def update_webhook(webhook_id: str, payload: WebhookUpdate, request: _Request):
    """Update a registered webhook (URL, events, or active status).

    Use this to re-enable a webhook that was auto-disabled after consecutive failures.
    When re-enabling (`is_active: true`), `consecutive_failures` is reset to 0.
    """
    wh = await store.get_webhook(webhook_id)
    if wh is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")

    updates: dict = {}
    if payload.url is not None:
        await _block_ssrf(payload.url)
        updates["url"] = payload.url
    if payload.events is not None:
        # Validate events
        if payload.events != ["*"]:
            unknown = [e for e in payload.events if e not in WEBHOOK_EVENTS]
            if unknown:
                raise HTTPException(status_code=422, detail=f"Unknown event(s): {unknown!r}")
        updates["events"] = payload.events
    if payload.is_active is not None:
        updates["is_active"] = payload.is_active
        if payload.is_active:
            updates["consecutive_failures"] = 0  # reset on re-enable

    if not updates:
        return _to_response(wh)

    wh = await store.update_webhook(webhook_id, **updates)
    if wh is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")

    import asyncio as _asyncio
    from app.services.audit_log_service import log_event as _audit
    _asyncio.create_task(_audit(
        account_id=None,
        action="webhook.updated",
        resource_type="webhook",
        resource_id=webhook_id,
        ip_address=request.client.host if request.client else None,
        details=updates,
    ))

    return _to_response(wh)


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(webhook_id: str):
    wh = await store.get_webhook(webhook_id)
    if wh is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")
    return _to_response(wh)


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: str):
    if not await store.delete_webhook(webhook_id):
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")

    import asyncio as _asyncio
    from app.services.audit_log_service import log_event as _audit
    _asyncio.create_task(_audit(
        account_id=None,
        action="webhook.deleted",
        resource_type="webhook",
        resource_id=webhook_id,
    ))


@router.post("/{webhook_id}/test")
@_limiter.limit("5/minute")
async def test_webhook(webhook_id: str, request: _Request):
    """Send a test event to this webhook endpoint."""
    wh = await store.get_webhook(webhook_id)
    if wh is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")
    await _block_ssrf(wh.url)

    from app.services.webhook_service import _attempt_delivery, _build_body, _sign
    body = _build_body("bot.test", {"message": "Test delivery from JustHereToListen.io"})
    headers = {"Content-Type": "application/json", "User-Agent": "JustHereToListen.io/1.0"}
    if wh.secret:
        sig, ts = _sign(body, wh.secret)
        headers["X-MeetingBot-Signature"] = sig
        headers["X-MeetingBot-Timestamp"] = ts

    status_code, _ = await _attempt_delivery(wh.url, body, headers)
    if status_code is None:
        raise HTTPException(status_code=502, detail="Test delivery failed — endpoint unreachable or returned 5xx")
    return {"status_code": status_code, "url": wh.url}


@router.get("/{webhook_id}/deliveries")
async def list_deliveries(
    webhook_id: str,
    limit: int = 50,
    offset: int = 0,
):
    """List delivery log entries for a registered webhook.

    Returns a paginated envelope. Entries are sorted newest-first. Each entry
    includes the attempt status, HTTP response code, error message, and next retry time.
    """
    wh = await store.get_webhook(webhook_id)
    if wh is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")

    try:
        from app.db import AsyncSessionLocal
        from app.models.account import WebhookDelivery
        from sqlalchemy import select, func
        async with AsyncSessionLocal() as session:
            total_result = await session.execute(
                select(func.count(WebhookDelivery.id))
                .where(WebhookDelivery.webhook_id == webhook_id)
            )
            total = total_result.scalar() or 0
            result = await session.execute(
                select(WebhookDelivery)
                .where(WebhookDelivery.webhook_id == webhook_id)
                .order_by(WebhookDelivery.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = result.scalars().all()
    except Exception:
        logger.exception("Failed to list webhook deliveries for webhook %s", webhook_id)
        raise HTTPException(status_code=500, detail="Internal server error")

    items = [
        DeliveryResponse(
            id=r.id,
            webhook_id=r.webhook_id,
            bot_id=r.bot_id,
            event=r.event,
            status=r.status,
            attempt_number=r.attempt_number,
            response_status_code=r.response_status_code,
            response_body=r.response_body,
            error_message=r.error_message,
            next_retry_at=r.next_retry_at,
            delivered_at=r.delivered_at,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return {
        "results": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }
