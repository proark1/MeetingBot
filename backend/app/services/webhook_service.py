"""Webhook delivery service — fires HTTP POST to registered endpoints.

v4.x additions:
- Every delivery attempt is logged to the ``webhook_deliveries`` table.
- Failed deliveries are scheduled for retry with exponential back-off.
- A background loop (started by main.py lifespan) processes the retry queue.
"""

import asyncio
from collections import OrderedDict
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.api.ws import manager as ws_manager
from app.config import settings

logger = logging.getLogger(__name__)

_http_client: httpx.AsyncClient | None = None

# Per-webhook locks to prevent concurrent state mutation race conditions.
# Uses an LRU-bounded dict to prevent unbounded memory growth.
_WEBHOOK_LOCK_MAX = 500
_webhook_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()


def _get_webhook_lock(wh_id: str) -> asyncio.Lock:
    if wh_id in _webhook_locks:
        # Move to end (most recently used)
        _webhook_locks.move_to_end(wh_id)
        return _webhook_locks[wh_id]
    lock = asyncio.Lock()
    _webhook_locks[wh_id] = lock
    # Evict oldest if over limit — skip locks that are currently held
    while len(_webhook_locks) > _WEBHOOK_LOCK_MAX:
        oldest_key = next(iter(_webhook_locks))
        oldest_lock = _webhook_locks[oldest_key]
        if oldest_lock.locked():
            break  # don't evict a lock that's in use
        _webhook_locks.pop(oldest_key)
    return lock


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


def _retry_delays() -> list[int]:
    try:
        return [int(x.strip()) for x in settings.WEBHOOK_RETRY_DELAYS.split(",") if x.strip()]
    except Exception:
        return [60, 300, 1500, 7200, 36000]


# Cached at module load — settings don't change at runtime
_RETRY_DELAYS: list[int] = _retry_delays()


def _build_body(event: str, payload: dict) -> str:
    return json.dumps({"event": event, "data": payload, "ts": datetime.now(timezone.utc).isoformat()})


def _sign(body: str, secret: str) -> tuple[str, str]:
    """Return (signature, timestamp_str). Timestamp is included in the signed payload.

    BREAKING CHANGE: Signed payload is f"{timestamp}.{body}".
    Reject deliveries where abs(time.time() - int(X-MeetingBot-Timestamp)) > 300 seconds.
    """
    ts = str(int(datetime.now(timezone.utc).timestamp()))
    sig = "sha256=" + hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return sig, ts


# ── Delivery logging ───────────────────────────────────────────────────────────

async def _log_delivery(
    webhook_id: str,
    bot_id: "str | None",
    event: str,
    request_body: str,
    status: str = "pending",
    attempt_number: int = 1,
    response_status_code: "int | None" = None,
    response_body: "str | None" = None,
    error_message: "str | None" = None,
    next_retry_at: "datetime | None" = None,
    delivered_at: "datetime | None" = None,
    delivery_id: "str | None" = None,
) -> str:
    """Insert or update a WebhookDelivery row.  Returns the delivery id."""
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import WebhookDelivery
        async with AsyncSessionLocal() as session:
            if delivery_id:
                from sqlalchemy import select
                result = await session.execute(
                    select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
                )
                row = result.scalar_one_or_none()
                if row:
                    row.status               = status
                    row.attempt_number       = attempt_number
                    row.response_status_code = response_status_code
                    row.response_body        = (response_body or "")[:2000]
                    row.error_message        = error_message
                    row.next_retry_at        = next_retry_at
                    row.delivered_at         = delivered_at
                    await session.commit()
                    return delivery_id
            row = WebhookDelivery(
                webhook_id=webhook_id,
                bot_id=bot_id,
                event=event,
                request_body=request_body[:10000],
                status=status,
                attempt_number=attempt_number,
                response_status_code=response_status_code,
                response_body=(response_body or "")[:2000],
                error_message=error_message,
                next_retry_at=next_retry_at,
                delivered_at=delivered_at,
            )
            session.add(row)
            await session.commit()
            return row.id
    except Exception as exc:
        logger.debug("Could not log webhook delivery: %s", exc)
        return delivery_id or ""


# ── Single-attempt HTTP delivery ───────────────────────────────────────────────

async def _attempt_delivery(url: str, body: str, headers: dict) -> "tuple[int | None, str]":
    """Fire a single HTTP POST.  Returns (status_code, response_text)."""
    client = _get_client()
    try:
        resp = await client.post(url, content=body, headers=headers)
        return resp.status_code, resp.text[:2000]
    except Exception as exc:
        return None, str(exc)


# ── Core dispatch ──────────────────────────────────────────────────────────────

async def dispatch_event(
    event: str,
    payload: dict,
    extra_webhook_url: "str | None" = None,
    account_id: "str | None" = None,
) -> None:
    """Broadcast to WebSocket clients and all active registered webhooks."""
    await ws_manager.broadcast(event, payload, account_id=account_id)

    from app.store import store

    body = _build_body(event, payload)
    headers_base = {"Content-Type": "application/json", "User-Agent": "JustHereToListen.io/1.0"}
    bot_id: "str | None" = payload.get("id") or payload.get("bot_id")

    for wh in await store.active_webhooks():
        subscribed = wh.events == ["*"] or "*" in wh.events or event in wh.events
        if not subscribed:
            continue

        async with _get_webhook_lock(wh.id):
            hdrs = dict(headers_base)
            if wh.secret:
                sig, ts = _sign(body, wh.secret)
                hdrs["X-MeetingBot-Signature"] = sig
                hdrs["X-MeetingBot-Timestamp"] = ts

            delivery_id = await _log_delivery(
                webhook_id=wh.id, bot_id=bot_id, event=event,
                request_body=body, status="pending",
            )

            status_code, resp_text = await _attempt_delivery(wh.url, body, hdrs)
            now = datetime.now(timezone.utc)

            if status_code is not None and status_code < 500:
                await _log_delivery(
                    webhook_id=wh.id, bot_id=bot_id, event=event,
                    request_body=body, status="success", attempt_number=1,
                    response_status_code=status_code, response_body=resp_text,
                    delivered_at=now, delivery_id=delivery_id,
                )
                logger.info("Webhook delivered  wh=%s  url=%s  status=%d", wh.id, wh.url, status_code)
                wh.consecutive_failures = 0
            else:
                delays = _RETRY_DELAYS
                next_retry_at = now + timedelta(seconds=delays[0]) if delays else None
                await _log_delivery(
                    webhook_id=wh.id, bot_id=bot_id, event=event,
                    request_body=body, status="retrying", attempt_number=1,
                    response_status_code=status_code, response_body=resp_text,
                    error_message=resp_text if status_code is None else None,
                    next_retry_at=next_retry_at, delivery_id=delivery_id,
                )
                logger.warning(
                    "Webhook delivery failed  wh=%s  url=%s  status=%s — retry at %s",
                    wh.id, wh.url, status_code, next_retry_at,
                )
                wh.consecutive_failures = (wh.consecutive_failures or 0) + 1
                if wh.consecutive_failures >= 5:
                    wh.is_active = False
                    logger.warning("Webhook %s auto-disabled (5 consecutive failures)", wh.id)
                    try:
                        from app.services.audit_log_service import log_event as _audit_log
                        asyncio.create_task(_audit_log(
                            account_id=None,
                            action="webhook.auto_disabled",
                            resource_type="webhook",
                            resource_id=wh.id,
                            details={"url": wh.url, "consecutive_failures": wh.consecutive_failures},
                        ))
                    except Exception:
                        pass

            wh.delivery_attempts += 1
            wh.last_delivery_at = now
            wh.last_delivery_status = status_code
            await store._persist_webhook(wh)

    # Per-bot best-effort webhook (no retry, no DB log)
    if extra_webhook_url:
        asyncio.create_task(_fire_extra_webhook(extra_webhook_url, body, headers_base, event))


async def _fire_extra_webhook(url: str, body: str, headers: dict, event: str) -> None:
    status_code, _ = await _attempt_delivery(url, body, headers)
    if status_code and status_code < 500:
        logger.info("Per-bot webhook ok  url=%s  event=%s  status=%d", url, event, status_code)
    else:
        logger.warning("Per-bot webhook failed  url=%s  event=%s  status=%s", url, event, status_code)


# ── Retry background loop ──────────────────────────────────────────────────────

async def webhook_retry_loop(interval_s: int = 30) -> None:
    """Background task: process pending retries from the DB."""
    logger.info("Webhook retry loop started (interval=%ds)", interval_s)
    while True:
        try:
            await _process_retries()
        except Exception as exc:
            logger.error("Webhook retry loop error: %s", exc)
        await asyncio.sleep(interval_s)


async def _process_retries() -> None:
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import WebhookDelivery
        from sqlalchemy import select
    except ImportError:
        return

    now = datetime.now(timezone.utc)
    delays = _RETRY_DELAYS
    max_attempts = settings.WEBHOOK_MAX_ATTEMPTS

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.status == "retrying",
                WebhookDelivery.next_retry_at <= now,
                WebhookDelivery.attempt_number < max_attempts,
            ).limit(50)
        )
        pending: list[WebhookDelivery] = list(result.scalars().all())

    if not pending:
        return

    # Batch-fetch all needed webhooks in a single lock acquisition instead of N separate calls
    from app.store import store
    needed_ids = {d.webhook_id for d in pending}
    async with store._lock:
        webhooks_map = {wid: store._webhooks.get(wid) for wid in needed_ids}

    for delivery in pending:
        wh = webhooks_map.get(delivery.webhook_id)
        if wh is None or not wh.is_active:
            await _log_delivery(
                webhook_id=delivery.webhook_id, bot_id=delivery.bot_id,
                event=delivery.event, request_body=delivery.request_body,
                status="failed", attempt_number=delivery.attempt_number,
                error_message="Webhook no longer active", delivery_id=delivery.id,
            )
            continue

        hdrs: dict = {"Content-Type": "application/json", "User-Agent": "JustHereToListen.io/1.0"}
        if wh.secret:
            sig, ts = _sign(delivery.request_body, wh.secret)
            hdrs["X-MeetingBot-Signature"] = sig
            hdrs["X-MeetingBot-Timestamp"] = ts

        next_attempt = delivery.attempt_number + 1
        status_code, resp_text = await _attempt_delivery(wh.url, delivery.request_body, hdrs)
        retry_now = datetime.now(timezone.utc)

        if status_code is not None and status_code < 500:
            await _log_delivery(
                webhook_id=delivery.webhook_id, bot_id=delivery.bot_id,
                event=delivery.event, request_body=delivery.request_body,
                status="success", attempt_number=next_attempt,
                response_status_code=status_code, response_body=resp_text,
                delivered_at=retry_now, delivery_id=delivery.id,
            )
            logger.info("Webhook retry succeeded  id=%s  attempt=%d  status=%d",
                        delivery.id, next_attempt, status_code)
        elif next_attempt >= max_attempts:
            await _log_delivery(
                webhook_id=delivery.webhook_id, bot_id=delivery.bot_id,
                event=delivery.event, request_body=delivery.request_body,
                status="failed", attempt_number=next_attempt,
                response_status_code=status_code, response_body=resp_text,
                error_message=f"Gave up after {max_attempts} attempts", delivery_id=delivery.id,
            )
            logger.error("Webhook permanently failed  id=%s  after %d attempts",
                         delivery.id, max_attempts)
        else:
            delay_idx = min(next_attempt - 1, len(delays) - 1)
            next_retry = retry_now + timedelta(seconds=delays[delay_idx])
            await _log_delivery(
                webhook_id=delivery.webhook_id, bot_id=delivery.bot_id,
                event=delivery.event, request_body=delivery.request_body,
                status="retrying", attempt_number=next_attempt,
                response_status_code=status_code, response_body=resp_text,
                next_retry_at=next_retry, delivery_id=delivery.id,
            )
            logger.warning("Webhook retry scheduled  id=%s  attempt=%d/%d  next=%s",
                           delivery.id, next_attempt, max_attempts, next_retry)


async def prune_old_deliveries() -> int:
    """Delete delivery log entries older than WEBHOOK_DELIVERY_RETENTION_DAYS."""
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import WebhookDelivery
        from sqlalchemy import delete as sa_delete
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.WEBHOOK_DELIVERY_RETENTION_DAYS)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                sa_delete(WebhookDelivery).where(WebhookDelivery.created_at < cutoff)
            )
            await session.commit()
            return result.rowcount or 0
    except Exception as exc:
        logger.debug("Delivery log pruning failed: %s", exc)
        return 0
