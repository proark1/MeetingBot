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
import ipaddress
import json
import logging
import socket
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import httpx

from app.api.ws import manager as ws_manager
from app.config import settings

logger = logging.getLogger(__name__)

_http_client: httpx.AsyncClient | None = None

# Per-webhook locks to prevent concurrent state mutation race conditions.
# Uses an LRU-bounded dict to prevent unbounded memory growth.
_WEBHOOK_LOCK_MAX = 500
_webhook_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()

# Strong refs for fire-and-forget audit-log tasks so they aren't GC'd mid-await.
_bg_audit_tasks: "set[asyncio.Task]" = set()


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


def _classify_status(status_code: "int | None") -> str:
    """Classify an HTTP delivery result.

    Returns one of:
      ``"success"``  — 2xx/3xx, mark delivered, reset failure counter
      ``"retry"``    — transient error (timeout, 5xx, 408, 429); retry later
      ``"fail"``     — permanent client error (other 4xx); don't retry, count toward auto-disable
    """
    if status_code is None:
        return "retry"  # connection/timeout/SSRF — retry later
    if 200 <= status_code < 400:
        return "success"
    if status_code >= 500 or status_code in (408, 425, 429):
        return "retry"
    return "fail"  # 400/401/403/404/etc — receiver permanently rejects payload


def _build_body(event: str, payload: dict, ts_iso: "str | None" = None) -> str:
    if ts_iso is None:
        ts_iso = datetime.now(timezone.utc).isoformat()
    return json.dumps({"event": event, "data": payload, "ts": ts_iso})


def _sign(body: str, secret: str, ts_unix: "str | None" = None) -> tuple[str, str]:
    """Return (signature, timestamp_str). Timestamp is included in the signed payload.

    BREAKING CHANGE: Signed payload is f"{timestamp}.{body}".
    Reject deliveries where abs(time.time() - int(X-MeetingBot-Timestamp)) > 300 seconds.
    """
    if ts_unix is None:
        ts_unix = str(int(datetime.now(timezone.utc).timestamp()))
    sig = "sha256=" + hmac.new(secret.encode(), f"{ts_unix}.{body}".encode(), hashlib.sha256).hexdigest()
    return sig, ts_unix


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


# ── SSRF protection (shared with api.webhooks) ─────────────────────────────────

# Private/reserved IP ranges that webhook traffic must never reach.
# Includes cloud metadata endpoints (169.254.169.254) — AWS/GCP/Azure IMDS.
SSRF_BLOCKED_NETS: list = [ipaddress.ip_network(n) for n in [
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
SSRF_ALLOWED_SCHEMES = {"http", "https"}


def _is_blocked_ip(addr) -> bool:
    return addr.is_private or addr.is_loopback or addr.is_link_local or any(
        addr in net for net in SSRF_BLOCKED_NETS
    )


async def check_url_ssrf(url: str) -> "str | None":
    """Resolve a URL's hostname and return ``None`` if safe, else an error string.

    Defends against DNS-rebinding/TOCTOU SSRF: every delivery resolves the host
    fresh and verifies the IP isn't private/loopback/cloud-metadata. Note: there
    is still a tiny window between this resolution and httpx's own resolution
    where DNS could flip — pinning the connection IP would close it fully and
    is left as a future hardening step.
    """
    parsed = urlparse(url)
    if parsed.scheme not in SSRF_ALLOWED_SCHEMES:
        return f"scheme {parsed.scheme!r} not allowed"
    host = parsed.hostname or ""
    if not host or host.lower() in ("localhost", "0.0.0.0"):
        return "host targets localhost"

    # IP literal — check directly
    try:
        addr = ipaddress.ip_address(host)
        if _is_blocked_ip(addr):
            return f"address {host} is private/reserved"
        return None
    except ValueError:
        pass  # hostname — resolve below

    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, None),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        return "DNS resolution timed out"
    except socket.gaierror as exc:
        return f"DNS resolution failed: {exc}"

    for *_, sockaddr in infos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_blocked_ip(addr):
            return f"resolves to private/reserved address ({sockaddr[0]})"
    return None


# ── Single-attempt HTTP delivery ───────────────────────────────────────────────

async def _attempt_delivery(url: str, body: str, headers: dict) -> "tuple[int | None, str]":
    """Fire a single HTTP POST.  Returns (status_code, response_text).

    Re-validates the URL's resolved IP at delivery time (DNS-rebinding /
    TOCTOU defense) — registration-time validation isn't enough because DNS
    can change between register and fire.
    """
    ssrf_err = await check_url_ssrf(url)
    if ssrf_err is not None:
        logger.warning("Webhook delivery blocked by SSRF guard  url=%s  reason=%s", url, ssrf_err)
        return None, f"SSRF blocked: {ssrf_err}"

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

    _now_dt = datetime.now(timezone.utc)
    body = _build_body(event, payload, ts_iso=_now_dt.isoformat())
    _ts_unix = str(int(_now_dt.timestamp()))
    headers_base = {"Content-Type": "application/json", "User-Agent": "JustHereToListen.io/1.0"}
    bot_id: "str | None" = payload.get("id") or payload.get("bot_id")

    # Tenant scoping: only deliver to webhooks owned by the originating
    # account (plus any legacy global webhooks with account_id=None).
    for wh in await store.active_webhooks(account_id=account_id):
        subscribed = wh.events == ["*"] or "*" in wh.events or event in wh.events
        if not subscribed:
            continue

        async with _get_webhook_lock(wh.id):
            hdrs = dict(headers_base)
            if wh.secret:
                sig, ts = _sign(body, wh.secret, ts_unix=_ts_unix)
                hdrs["X-MeetingBot-Signature"] = sig
                hdrs["X-MeetingBot-Timestamp"] = ts

            delivery_id = await _log_delivery(
                webhook_id=wh.id, bot_id=bot_id, event=event,
                request_body=body, status="pending",
            )

            status_code, resp_text = await _attempt_delivery(wh.url, body, hdrs)
            now = datetime.now(timezone.utc)
            outcome = _classify_status(status_code)

            if outcome == "success":
                await _log_delivery(
                    webhook_id=wh.id, bot_id=bot_id, event=event,
                    request_body=body, status="success", attempt_number=1,
                    response_status_code=status_code, response_body=resp_text,
                    delivered_at=now, delivery_id=delivery_id,
                )
                logger.info("Webhook delivered  wh=%s  url=%s  status=%d", wh.id, wh.url, status_code)
                wh.consecutive_failures = 0
            elif outcome == "retry":
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
            else:  # "fail" — permanent client error (4xx other than 408/429)
                await _log_delivery(
                    webhook_id=wh.id, bot_id=bot_id, event=event,
                    request_body=body, status="failed", attempt_number=1,
                    response_status_code=status_code, response_body=resp_text,
                    error_message=f"Receiver rejected with HTTP {status_code} (not retried)",
                    delivery_id=delivery_id,
                )
                logger.warning(
                    "Webhook permanently rejected  wh=%s  url=%s  status=%s",
                    wh.id, wh.url, status_code,
                )
                wh.consecutive_failures = (wh.consecutive_failures or 0) + 1

            if outcome != "success" and wh.consecutive_failures >= 5:
                wh.is_active = False
                logger.warning("Webhook %s auto-disabled (5 consecutive failures)", wh.id)
                try:
                    from app.services.audit_log_service import log_event as _audit_log
                    _bg = asyncio.create_task(_audit_log(
                        account_id=wh.account_id,
                        action="webhook.auto_disabled",
                        resource_type="webhook",
                        resource_id=wh.id,
                        details={"url": wh.url, "consecutive_failures": wh.consecutive_failures},
                    ))
                    _bg_audit_tasks.add(_bg)
                    _bg.add_done_callback(_bg_audit_tasks.discard)
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

    from app.store import store

    for delivery in pending:
        # Per-webhook lock held for the full read→deliver→update→persist
        # iteration so concurrent PATCH/DELETE can't race in stale state.
        async with _get_webhook_lock(delivery.webhook_id):
            wh = await store.get_webhook(delivery.webhook_id)
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
            outcome = _classify_status(status_code)

            if outcome == "success":
                await _log_delivery(
                    webhook_id=delivery.webhook_id, bot_id=delivery.bot_id,
                    event=delivery.event, request_body=delivery.request_body,
                    status="success", attempt_number=next_attempt,
                    response_status_code=status_code, response_body=resp_text,
                    delivered_at=retry_now, delivery_id=delivery.id,
                )
                logger.info("Webhook retry succeeded  id=%s  attempt=%d  status=%d",
                            delivery.id, next_attempt, status_code)
                wh.consecutive_failures = 0
            elif outcome == "fail" or next_attempt >= max_attempts:
                err_msg = (
                    f"Receiver rejected with HTTP {status_code} (not retried)"
                    if outcome == "fail"
                    else f"Gave up after {max_attempts} attempts"
                )
                await _log_delivery(
                    webhook_id=delivery.webhook_id, bot_id=delivery.bot_id,
                    event=delivery.event, request_body=delivery.request_body,
                    status="failed", attempt_number=next_attempt,
                    response_status_code=status_code, response_body=resp_text,
                    error_message=err_msg, delivery_id=delivery.id,
                )
                logger.error("Webhook permanently failed  id=%s  after %d attempts",
                             delivery.id, next_attempt)
                wh.consecutive_failures = (wh.consecutive_failures or 0) + 1
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
                wh.consecutive_failures = (wh.consecutive_failures or 0) + 1

            if outcome != "success" and wh.consecutive_failures >= 5:
                wh.is_active = False
                logger.warning("Webhook %s auto-disabled (5 consecutive failures)", wh.id)

            wh.delivery_attempts += 1
            wh.last_delivery_at = retry_now
            wh.last_delivery_status = status_code
            await store._persist_webhook(wh)


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
