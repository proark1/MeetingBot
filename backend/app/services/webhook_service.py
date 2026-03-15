"""Webhook delivery service — fires HTTP POST to registered endpoints."""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx

from app.api.ws import manager as ws_manager
from app.config import settings

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAYS = [1, 3, 8]

_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=settings.WEBHOOK_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()


async def _deliver_with_retry(
    client: httpx.AsyncClient,
    url: str,
    body: str,
    headers: dict,
) -> int | None:
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


def _build_body(event: str, payload: dict) -> str:
    return json.dumps({"event": event, "data": payload, "ts": datetime.now(timezone.utc).isoformat()})


def _sign(body: str, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


async def dispatch_event(
    event: str,
    payload: dict,
    extra_webhook_url: str | None = None,
) -> None:
    """Broadcast to WebSocket clients and all active registered webhooks.

    If `extra_webhook_url` is set (per-bot webhook), it is also called.
    """
    # WebSocket — instant, best-effort
    await ws_manager.broadcast(event, payload)

    from app.store import store

    body = _build_body(event, payload)
    client = _get_client()
    headers_base = {"Content-Type": "application/json", "User-Agent": "MeetingBot/1.0"}

    # Build list of (url, optional_secret, webhook_entry_or_none)
    targets: list[tuple[str, str | None, object | None]] = []

    for wh in store.active_webhooks():
        subscribed = wh.events == ["*"] or "*" in wh.events or event in wh.events
        if subscribed:
            targets.append((wh.url, wh.secret, wh))

    if extra_webhook_url:
        targets.append((extra_webhook_url, None, None))

    if not targets:
        return

    async def _deliver_one(url: str, secret: str | None, wh_entry) -> None:
        headers = dict(headers_base)
        if secret:
            headers["X-MeetingBot-Signature"] = _sign(body, secret)
        status_code = await _deliver_with_retry(client, url, body, headers)
        # Update stats on global webhook entries
        if wh_entry is not None:
            wh_entry.delivery_attempts += 1
            wh_entry.last_delivery_at = datetime.now(timezone.utc)
            wh_entry.last_delivery_status = status_code
            if status_code is None or status_code >= 500:
                wh_entry.consecutive_failures = (wh_entry.consecutive_failures or 0) + 1
                if wh_entry.consecutive_failures >= 5:
                    wh_entry.is_active = False
                    logger.warning(
                        "Webhook %s auto-disabled after %d consecutive failures",
                        wh_entry.id, wh_entry.consecutive_failures,
                    )
            else:
                wh_entry.consecutive_failures = 0

    await asyncio.gather(
        *(_deliver_one(url, secret, wh) for url, secret, wh in targets),
        return_exceptions=True,
    )
