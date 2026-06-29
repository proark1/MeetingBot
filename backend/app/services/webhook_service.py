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
import ssl
import socket
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from app.api.ws import manager as ws_manager
from app.config import settings

logger = logging.getLogger(__name__)

# Per-webhook locks to prevent concurrent state mutation race conditions.
# Uses an LRU-bounded dict to prevent unbounded memory growth.
_WEBHOOK_LOCK_MAX = 500
_webhook_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()

# Strong refs for fire-and-forget audit-log tasks so they aren't GC'd mid-await.
_bg_audit_tasks: "set[asyncio.Task]" = set()

# Bound on concurrent webhook deliveries fanned out per event / retry batch.
_WEBHOOK_DELIVERY_CONCURRENCY = 10

# Short-TTL cache of successful (safe) DNS resolutions, keyed by host. Avoids
# re-resolving the same endpoint on every delivery attempt during a live
# meeting. Only safe results are cached — blocked/failed resolutions are never
# cached, so SSRF protection is never weakened.
# TTL is kept short so a DNS-rebind to a private IP is re-detected quickly on
# subsequent deliveries (the positive cache otherwise re-approves a host for its
# whole TTL). It still covers a single event's webhook fan-out, which fires
# within milliseconds. Full TOCTOU closure would require pinning the validated
# IP into the connection (careful TLS-SNI handling) — a separate hardening step.
_DNS_CACHE_TTL_S = 10.0
# Cap the positive DNS cache so it can't grow without bound across many distinct
# webhook hostnames (it was previously only TTL-evicted on re-lookup of the same
# host). Insertion-ordered dict → evict oldest entries when over the cap.
_DNS_CACHE_MAX = 1024
_dns_cache: "dict[str, float]" = {}


def _get_webhook_lock(wh_id: str) -> asyncio.Lock:
    if wh_id in _webhook_locks:
        # Move to end (most recently used)
        _webhook_locks.move_to_end(wh_id)
        return _webhook_locks[wh_id]
    lock = asyncio.Lock()
    _webhook_locks[wh_id] = lock
    # Evict oldest if over limit — scan past held locks instead of stopping at
    # the first one. A `break` here let a single stuck delivery prevent any
    # eviction, allowing the dict to grow unbounded under sustained load.
    if len(_webhook_locks) > _WEBHOOK_LOCK_MAX:
        scanned = 0
        max_scan = len(_webhook_locks)  # bound by current size to avoid a tight loop
        for oldest_key in list(_webhook_locks.keys()):
            if len(_webhook_locks) <= _WEBHOOK_LOCK_MAX:
                break
            scanned += 1
            if scanned > max_scan:
                break
            oldest_lock = _webhook_locks[oldest_key]
            if oldest_lock.locked() or oldest_key == wh_id:
                continue  # skip held locks and the one we just inserted
            _webhook_locks.pop(oldest_key, None)
    return lock


async def close_http_client() -> None:
    """Compatibility shutdown hook; pinned webhook delivery owns no async client."""
    return None


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
      ``"success"``  — 2xx, mark delivered, reset failure counter
      ``"retry"``    — transient error (timeout, 5xx, 408, 429); retry later
      ``"fail"``     — permanent client error (other 4xx, 3xx with redirects
                       disabled); don't retry, count toward auto-disable
    """
    if status_code is None:
        return "retry"  # connection error / network timeout — retry later
    if 200 <= status_code < 300:
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
    signed_ts: "str | None" = None,
) -> str:
    """Insert or update a WebhookDelivery row.  Returns the delivery id.

    ``signed_ts`` is the original Unix-seconds timestamp baked into the body
    envelope and the HMAC payload. It MUST be persisted on the first attempt
    so retries reuse the same value — otherwise the X-MeetingBot-Timestamp
    header drifts apart from ``data.ts`` and receivers reject the redelivery.
    """
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
                    if signed_ts is not None and not row.signed_ts:
                        row.signed_ts = signed_ts
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
                signed_ts=signed_ts,
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
SSRF_LOCALHOST_NAMES = {"localhost", ipaddress.ip_address(0).compressed}


def _is_blocked_ip(addr) -> bool:
    return addr.is_private or addr.is_loopback or addr.is_link_local or any(
        addr in net for net in SSRF_BLOCKED_NETS
    )


async def check_url_ssrf(url: str) -> "str | None":
    """Resolve a URL's hostname and return ``None`` if safe, else an error string.

    Used at registration/update time. Delivery uses a stricter path that
    resolves the host, validates every returned address, and connects to the
    exact validated IP for the HTTP attempt.
    """
    parsed = urlparse(url)
    if parsed.scheme not in SSRF_ALLOWED_SCHEMES:
        return f"scheme {parsed.scheme!r} not allowed"
    host = parsed.hostname or ""
    if not host or host.lower() in SSRF_LOCALHOST_NAMES:
        return "host targets localhost"

    # IP literal — check directly
    try:
        addr = ipaddress.ip_address(host)
        if _is_blocked_ip(addr):
            return f"address {host} is private/reserved"
        return None
    except ValueError:
        pass  # hostname — resolve below

    # Serve from cache only if a previous resolution was verified safe and is
    # still within the TTL window. Expired/missing entries fall through to a
    # fresh resolution; only safe results are ever written back to the cache.
    cached_until = _dns_cache.get(host)
    now_mono = asyncio.get_event_loop().time()
    if cached_until is not None:
        if cached_until > now_mono:
            return None
        _dns_cache.pop(host, None)

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
    # Bound the cache: drop the oldest entries once over the cap (refreshing an
    # existing host first so re-resolved hosts move to the end / stay hot).
    _dns_cache.pop(host, None)
    _dns_cache[host] = now_mono + _DNS_CACHE_TTL_S
    while len(_dns_cache) > _DNS_CACHE_MAX:
        _dns_cache.pop(next(iter(_dns_cache)), None)
    return None


# ── Single-attempt HTTP delivery ───────────────────────────────────────────────

def _header_host(parsed) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        return f"{host}:{parsed.port}"
    return host


def _post_pinned_sync(url: str, body: str, headers: dict, ip: str) -> "tuple[int, str]":
    """POST to a pre-validated IP while preserving HTTP Host and TLS SNI.

    httpx validates a host, then performs its own DNS lookup later. A malicious
    DNS server can rebind between those two steps. This small HTTP/1.1 client
    connects to the exact IP address we already validated, while HTTPS still
    verifies the certificate against the original hostname via SNI.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"

    body_bytes = body.encode()
    request_headers = {
        "Host": _header_host(parsed),
        "Content-Length": str(len(body_bytes)),
        "Connection": "close",
        **headers,
    }
    header_blob = "".join(
        f"{name}: {str(value).replace(chr(13), '').replace(chr(10), '')}\r\n"
        for name, value in request_headers.items()
    )
    request = (
        f"POST {target} HTTP/1.1\r\n"
        f"{header_blob}\r\n"
    ).encode() + body_bytes

    timeout = max(1, int(settings.WEBHOOK_TIMEOUT_SECONDS))
    with socket.create_connection((ip, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        if parsed.scheme == "https":
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
                tls_sock.sendall(request)
                raw = _recv_response(tls_sock)
        else:
            sock.sendall(request)
            raw = _recv_response(sock)

    header_part, _, response_body = raw.partition(b"\r\n\r\n")
    status_line = header_part.splitlines()[0].decode("iso-8859-1", errors="replace")
    try:
        status_code = int(status_line.split()[1])
    except Exception as exc:
        raise RuntimeError(f"Malformed HTTP response: {status_line!r}") from exc
    return status_code, response_body[:2000].decode("utf-8", errors="replace")


def _recv_response(sock) -> bytes:
    chunks: list[bytes] = []
    total = 0
    max_bytes = 65536
    while total < max_bytes:
        chunk = sock.recv(min(8192, max_bytes - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


async def _resolve_public_ips(url: str) -> "tuple[list[str], str | None]":
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme not in SSRF_ALLOWED_SCHEMES:
        return [], f"scheme {parsed.scheme!r} not allowed"
    if not host or host.lower() in SSRF_LOCALHOST_NAMES:
        return [], "host targets localhost"

    try:
        addr = ipaddress.ip_address(host)
        if _is_blocked_ip(addr):
            return [], f"address {host} is private/reserved"
        return [str(addr)], None
    except ValueError:
        pass

    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80)),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        return [], "DNS resolution timed out"
    except socket.gaierror as exc:
        return [], f"DNS resolution failed: {exc}"

    ips: list[str] = []
    for *_, sockaddr in infos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_blocked_ip(addr):
            return [], f"resolves to private/reserved address ({sockaddr[0]})"
        if str(addr) not in ips:
            ips.append(str(addr))
    if not ips:
        return [], "DNS resolution returned no usable addresses"
    return ips, None


async def _attempt_delivery(url: str, body: str, headers: dict) -> "tuple[int | None, str]":
    """Fire a single HTTP POST.  Returns (status_code, response_text).

    Re-validates the URL's resolved IP at delivery time (DNS-rebinding /
    TOCTOU defense) — registration-time validation isn't enough because DNS
    can change between register and fire.
    """
    ips, ssrf_err = await _resolve_public_ips(url)
    if ssrf_err is not None:
        logger.warning("Webhook delivery blocked by SSRF guard  url=%s  reason=%s", url, ssrf_err)
        # Return 403 (not None) so _classify_status marks this as permanent "fail"
        # rather than "retry".  A blocked URL will not become unblocked on retry.
        return 403, f"SSRF blocked: {ssrf_err}"

    last_exc: Exception | None = None
    for ip in ips:
        try:
            return await asyncio.to_thread(_post_pinned_sync, url, body, headers, ip)
        except Exception as exc:
            last_exc = exc
            logger.debug("Pinned webhook POST failed url=%s ip=%s: %s", url, ip, exc)
    return None, str(last_exc) if last_exc else "delivery failed"


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
    sem = asyncio.Semaphore(_WEBHOOK_DELIVERY_CONCURRENCY)

    async def _deliver_one(wh) -> None:
        subscribed = wh.events == ["*"] or "*" in wh.events or event in wh.events
        if not subscribed:
            return

        async with sem, _get_webhook_lock(wh.id):
            hdrs = dict(headers_base)
            if wh.secret:
                sig, ts = _sign(body, wh.secret, ts_unix=_ts_unix)
                hdrs["X-MeetingBot-Signature"] = sig
                hdrs["X-MeetingBot-Timestamp"] = ts

            status_code, resp_text = await _attempt_delivery(wh.url, body, hdrs)
            now = datetime.now(timezone.utc)
            outcome = _classify_status(status_code)

            # Single-write the delivery log for terminal outcomes; only the
            # retry path needs a persisted row the retry processor will later
            # update by id (so signed_ts is carried only when retrying).
            if outcome == "success":
                await _log_delivery(
                    webhook_id=wh.id, bot_id=bot_id, event=event,
                    request_body=body, status="success", attempt_number=1,
                    response_status_code=status_code, response_body=resp_text,
                    delivered_at=now,
                )
                logger.info("Webhook delivered  wh=%s  url=%s  status=%d", wh.id, wh.url, status_code)
            elif outcome == "retry":
                delays = _RETRY_DELAYS
                next_retry_at = now + timedelta(seconds=delays[0]) if delays else None
                await _log_delivery(
                    webhook_id=wh.id, bot_id=bot_id, event=event,
                    request_body=body, status="retrying", attempt_number=1,
                    response_status_code=status_code, response_body=resp_text,
                    error_message=resp_text if status_code is None else None,
                    next_retry_at=next_retry_at, signed_ts=_ts_unix,
                )
                logger.warning(
                    "Webhook delivery failed  wh=%s  url=%s  status=%s — retry at %s",
                    wh.id, wh.url, status_code, next_retry_at,
                )
            else:  # "fail" — permanent client error (4xx other than 408/429)
                await _log_delivery(
                    webhook_id=wh.id, bot_id=bot_id, event=event,
                    request_body=body, status="failed", attempt_number=1,
                    response_status_code=status_code, response_body=resp_text,
                    error_message=f"Receiver rejected with HTTP {status_code} (not retried)",
                )
                logger.warning(
                    "Webhook permanently rejected  wh=%s  url=%s  status=%s",
                    wh.id, wh.url, status_code,
                )

            # Atomically bump counters + maybe auto-disable (no shared-object
            # mutation → no lost-increment race across deliveries/PATCH).
            cf, _active, auto_disabled = await store.record_webhook_delivery(
                wh.id, success=(outcome == "success"), status_code=status_code, now=now,
            )
            if auto_disabled:
                logger.warning("Webhook %s auto-disabled (5 consecutive failures)", wh.id)
                try:
                    from app.services.audit_log_service import log_event as _audit_log
                    _bg = asyncio.create_task(_audit_log(
                        account_id=wh.account_id,
                        action="webhook.auto_disabled",
                        resource_type="webhook",
                        resource_id=wh.id,
                        details={"url": wh.url, "consecutive_failures": cf},
                    ))
                    _bg_audit_tasks.add(_bg)
                    _bg.add_done_callback(_bg_audit_tasks.discard)
                except Exception:
                    pass

    await asyncio.gather(
        *(_deliver_one(wh) for wh in await store.active_webhooks(account_id=account_id)),
        return_exceptions=True,
    )

    # Per-bot best-effort webhook (no retry, no DB log) — tracked so the
    # task isn't GC'd mid-await before the HTTP POST completes.
    if extra_webhook_url:
        from app.services.background_tasks import tracked_task as _tracked
        _tracked(_fire_extra_webhook(extra_webhook_url, body, headers_base, event))


async def _fire_extra_webhook(url: str, body: str, headers: dict, event: str) -> None:
    try:
        status_code, _ = await _attempt_delivery(url, body, headers)
    except Exception as exc:
        # Never let a per-bot webhook failure escape this background task — it
        # would surface as an asyncio "Task exception was never retrieved"
        # warning with no audit trail and no operator-visible diagnostic.
        logger.warning("Per-bot webhook crashed  url=%s  event=%s  exc=%s", url, event, exc)
        return
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

    sem = asyncio.Semaphore(_WEBHOOK_DELIVERY_CONCURRENCY)

    async def _retry_one(delivery) -> None:
        # Per-webhook lock held for the full read→deliver→update→persist
        # iteration so concurrent PATCH/DELETE can't race in stale state.
        async with sem, _get_webhook_lock(delivery.webhook_id):
            wh = await store.get_webhook(delivery.webhook_id)
            if wh is None or not wh.is_active:
                await _log_delivery(
                    webhook_id=delivery.webhook_id, bot_id=delivery.bot_id,
                    event=delivery.event, request_body=delivery.request_body,
                    status="failed", attempt_number=delivery.attempt_number,
                    error_message="Webhook no longer active", delivery_id=delivery.id,
                )
                return

            hdrs: dict = {"Content-Type": "application/json", "User-Agent": "JustHereToListen.io/1.0"}
            if wh.secret:
                # Reuse the original ``signed_ts`` so the X-MeetingBot-Timestamp
                # header on each retry matches the ``ts`` field that was baked
                # into the persisted body when the delivery was first created.
                sig, ts = _sign(delivery.request_body, wh.secret, ts_unix=delivery.signed_ts)
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

            # Atomic counter update + auto-disable (see dispatch_event above).
            _cf, _active, auto_disabled = await store.record_webhook_delivery(
                wh.id, success=(outcome == "success"), status_code=status_code, now=retry_now,
            )
            if auto_disabled:
                logger.warning("Webhook %s auto-disabled (5 consecutive failures)", wh.id)

    await asyncio.gather(*(_retry_one(d) for d in pending), return_exceptions=True)


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
