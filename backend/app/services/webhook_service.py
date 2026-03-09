"""Webhook delivery service — fires HTTP POST to registered endpoints with retry."""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ws import manager as ws_manager
from app.config import settings
from app.models.webhook import Webhook

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAYS = [1, 3, 8]  # seconds between attempts

# Persistent client — reused across all webhook deliveries to avoid the
# connection-pool warmup cost of creating a new client on every status change.
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=settings.WEBHOOK_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    return _http_client


async def close_http_client() -> None:
    """Close the persistent client on app shutdown."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()


async def dispatch_event(
    db: AsyncSession,
    event: str,
    payload: dict,
) -> None:
    """Broadcast to WebSocket clients and deliver to all subscribed HTTP webhooks."""
    # WebSocket — instant, best-effort
    await ws_manager.broadcast(event, payload)

    # HTTP webhooks — with retry
    result = await db.execute(
        select(Webhook).where(Webhook.is_active == True)  # noqa: E712
    )
    webhooks = result.scalars().all()
    if not webhooks:
        return

    body = json.dumps(
        {"event": event, "data": payload, "ts": datetime.now(timezone.utc).isoformat()}
    )

    client = _get_client()
    for wh in webhooks:
        subscribed = wh.events == "*" or event in wh.events.split(",")
        if not subscribed:
            continue

        headers = {"Content-Type": "application/json", "User-Agent": "MeetingBot/1.0"}
        if wh.secret:
            sig = hmac.new(
                wh.secret.encode(), body.encode(), hashlib.sha256
            ).hexdigest()
            headers["X-MeetingBot-Signature"] = f"sha256={sig}"

        status_code = await _deliver_with_retry(client, wh.url, body, headers)
        wh.delivery_attempts += 1
        wh.last_delivery_at = datetime.now(timezone.utc)
        wh.last_delivery_status = status_code

        # Track consecutive failures; auto-disable after 5
        if status_code is None or status_code >= 500:
            wh.consecutive_failures = (wh.consecutive_failures or 0) + 1
            if wh.consecutive_failures >= 5:
                wh.is_active = False
                logger.warning(
                    "Webhook %s auto-disabled after %d consecutive failures (url=%s)",
                    wh.id, wh.consecutive_failures, wh.url,
                )
        else:
            wh.consecutive_failures = 0

    await db.commit()


async def _deliver_with_retry(
    client: httpx.AsyncClient,
    url: str,
    body: str,
    headers: dict,
) -> int | None:
    """Attempt delivery up to _MAX_RETRIES times with exponential backoff.
    Returns the final HTTP status code, or None if all attempts failed."""
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await client.post(url, content=body, headers=headers)
            if resp.status_code < 500:
                logger.info("Webhook delivered  url=%s  status=%d", url, resp.status_code)
                return resp.status_code
            logger.warning(
                "Webhook server error  url=%s  status=%d  attempt=%d/%d",
                url, resp.status_code, attempt + 1, _MAX_RETRIES,
            )
        except Exception as exc:
            logger.warning(
                "Webhook delivery error  url=%s  attempt=%d/%d  error=%s",
                url, attempt + 1, _MAX_RETRIES, exc,
            )

        if attempt < _MAX_RETRIES - 1:
            await asyncio.sleep(_RETRY_DELAYS[attempt])

    logger.error("Webhook delivery failed after %d attempts  url=%s", _MAX_RETRIES, url)
    return None
