"""Webhook registration API (per-account).

Webhooks registered here receive events for the authenticated account's bots.
For per-bot webhooks, pass `webhook_url` when creating a bot.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request as _Request
from slowapi import Limiter as _Limiter
from slowapi.util import get_remote_address as _get_remote_address

from app.deps import SUPERADMIN_ACCOUNT_ID
from app.schemas.webhook import WebhookCreate, WebhookResponse
from app.store import store, WebhookEntry

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
    "bot.decision_detected",
    "bot.coaching_tip",
    "bot.coaching_alert",
    "bot.speaker_analytics",
    "bot.agentic_action",
    "action_item.due_soon",
    "action_item.overdue",
    "bot.test",
]


async def _block_ssrf(url: str) -> None:
    """Reject webhook URLs that target internal/private infrastructure.

    Delegates to :func:`webhook_service.check_url_ssrf` so registration-time
    and delivery-time use identical rules. Returns 400 on any block reason
    except DNS resolution failure (transient — let delivery handle naturally).
    """
    from app.services.webhook_service import check_url_ssrf
    err = await check_url_ssrf(url)
    if err is None:
        return
    if err.startswith("DNS resolution failed"):
        return  # hostname unresolvable now — fail at delivery time
    raise HTTPException(status_code=400, detail=f"Webhook URL: {err}")


# Tracks fire-and-forget audit-log tasks so they aren't garbage-collected
# mid-await (which would silently swallow exceptions and drop log entries).
_audit_tasks: "set[asyncio.Task]" = set()


def _spawn_audit(coro) -> None:
    """Schedule an audit log coroutine without losing the task reference."""
    task = asyncio.create_task(coro)
    _audit_tasks.add(task)
    task.add_done_callback(_audit_tasks.discard)


def _audit_log(**kwargs):
    """Lazy import wrapper so audit_log_service isn't imported at module load."""
    from app.services.audit_log_service import log_event as _le
    return _le(**kwargs)


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
        account_id=wh.account_id,
    )


def _request_account_id(request: _Request) -> "str | None":
    """Account_id from the auth middleware, or None for superadmin/dev mode."""
    return getattr(request.state, "account_id", None)


async def _get_webhook_or_404(webhook_id: str, account_id: "str | None") -> WebhookEntry:
    """Fetch a webhook and enforce tenant ownership. 404 (not 403) on mismatch
    to avoid leaking webhook existence to other tenants."""
    wh = await store.get_webhook(webhook_id)
    if wh is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")
    if account_id and account_id != SUPERADMIN_ACCOUNT_ID and wh.account_id != account_id:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")
    return wh


@router.post("", response_model=WebhookResponse, status_code=201)
@_limiter.limit("10/minute")
async def create_webhook(payload: WebhookCreate, request: _Request):
    """Register a webhook scoped to the authenticated account.

    The webhook will receive events for all bots owned by this account:
    - `bot.joining`, `bot.in_call`, `bot.call_ended`
    - `bot.transcript_ready`, `bot.analysis_ready`
    - `bot.done`, `bot.error`, `bot.cancelled`

    Use `events: ["*"]` to receive all events (default), or list specific ones.

    For per-bot webhooks (results from a single bot), pass `webhook_url` when
    creating the bot instead.
    """
    await _block_ssrf(payload.url)
    account_id = _request_account_id(request)
    # Superadmin (legacy API_KEY) creates global webhooks → account_id=None.
    owner_id = None if account_id == SUPERADMIN_ACCOUNT_ID else account_id

    # Enforce per-account limit to prevent memory DoS via unbounded webhook creation.
    if owner_id is not None:
        existing = await store.list_webhooks(account_id=owner_id)
        if len(existing) >= 25:
            raise HTTPException(status_code=400, detail="Webhook limit reached (25 per account)")

    wh = await store.new_webhook(
        url=payload.url, events=payload.events, secret=payload.secret, account_id=owner_id
    )

    _spawn_audit(_audit_log(
        account_id=account_id,
        action="webhook.created",
        resource_type="webhook",
        resource_id=wh.id,
        ip_address=request.client.host if request.client else None,
        details={"url": wh.url, "events": wh.events},
    ))

    return _to_response(wh)


@router.get(
    "",
    responses={200: {"content": {"application/json": {"example": {
        "results": [{
            "id": "wh_5fa921b7",
            "url": "https://api.acme.com/justheretolisten/webhook",
            "events": ["bot.done", "bot.error"],
            "is_active": True,
            "created_at": "2026-05-04T12:00:00Z",
            "delivery_attempts": 124,
            "last_delivery_at": "2026-05-04T15:34:18Z",
            "last_delivery_status": 200,
            "consecutive_failures": 0,
            "account_id": "550e8400-e29b-41d4-a716-446655440000",
        }],
        "total": 1,
        "limit": 1,
        "offset": 0,
        "has_more": False,
    }}}}},
)
async def list_webhooks(request: _Request):
    """List webhooks owned by the authenticated account.

    Superadmin sees all webhooks. Returns a paginated envelope with `results`,
    `total`, and `has_more`.
    """
    account_id = _request_account_id(request)
    filter_account = None if account_id == SUPERADMIN_ACCOUNT_ID else account_id
    webhooks = await store.list_webhooks(account_id=filter_account)
    return {
        "results": [_to_response(wh) for wh in webhooks],
        "total": len(webhooks),
        "limit": len(webhooks),
        "offset": 0,
        "has_more": False,
    }


@router.get(
    "/events",
    responses={200: {"content": {"application/json": {"example": {
        "events": [
            "bot.joining", "bot.in_call", "bot.call_ended",
            "bot.transcript_ready", "bot.analysis_ready",
            "bot.done", "bot.error", "bot.cancelled",
            "bot.keyword_alert",
            "bot.live_transcript", "bot.live_transcript_translated", "bot.live_chat_message",
            "bot.recurring_intel_ready", "bot.test",
        ],
    }}}}},
)
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

    model_config = {"json_schema_extra": {"example": {
        "id": "wd_3fbc81a0",
        "webhook_id": "wh_5fa921b7",
        "bot_id": "bot_8a72c5e1",
        "event": "bot.done",
        "status": "delivered",
        "attempt_number": 1,
        "response_status_code": 200,
        "response_body": "ok",
        "error_message": None,
        "next_retry_at": None,
        "delivered_at": "2026-05-04T15:34:18Z",
        "created_at": "2026-05-04T15:34:18Z",
    }}}


@router.get(
    "/deliveries",
    tags=["Webhooks"],
    responses={200: {"content": {"application/json": {"example": {
        "results": [{
            "id": "wd_3fbc81a0",
            "webhook_id": "wh_5fa921b7",
            "bot_id": "bot_8a72c5e1",
            "event": "bot.done",
            "status": "delivered",
            "attempt_number": 1,
            "response_status_code": 200,
            "response_body": "ok",
            "error_message": None,
            "next_retry_at": None,
            "delivered_at": "2026-05-04T15:34:18Z",
            "created_at": "2026-05-04T15:34:18Z",
        }],
        "total": 1,
        "limit": 50,
        "offset": 0,
        "has_more": False,
    }}}}},
)
async def list_all_deliveries(
    request: _Request,
    limit: int = 50,
    offset: int = 0,
    event: _Opt[str] = None,
    status: _Opt[str] = None,
):
    """List recent webhook delivery attempts across the caller's webhooks.

    Superadmin sees all deliveries. Filter by ``event`` (e.g. ``bot.done``)
    or ``status`` (``pending`` / ``delivered`` / ``failed`` / ``retrying``).
    Returns a paginated envelope with ``results``, ``total``, ``limit``,
    ``offset``, and ``has_more``.
    """
    account_id = _request_account_id(request)
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import Webhook as _WebhookModel, WebhookDelivery
        from sqlalchemy import select, func
        async with AsyncSessionLocal() as session:
            base = select(WebhookDelivery)
            count_q = select(func.count(WebhookDelivery.id))
            if account_id and account_id != SUPERADMIN_ACCOUNT_ID:
                owned_ids = (
                    await session.execute(
                        select(_WebhookModel.id).where(_WebhookModel.account_id == account_id)
                    )
                ).scalars().all()
                if not owned_ids:
                    return {"results": [], "total": 0, "limit": limit, "offset": offset, "has_more": False}
                base = base.where(WebhookDelivery.webhook_id.in_(owned_ids))
                count_q = count_q.where(WebhookDelivery.webhook_id.in_(owned_ids))
            if event:
                base = base.where(WebhookDelivery.event == event)
                count_q = count_q.where(WebhookDelivery.event == event)
            if status:
                base = base.where(WebhookDelivery.status == status)
                count_q = count_q.where(WebhookDelivery.status == status)
            total = (await session.execute(count_q)).scalar() or 0
            result = await session.execute(
                base.order_by(WebhookDelivery.created_at.desc()).limit(limit).offset(offset)
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


@router.get("/{webhook_id}/deliveries", tags=["Webhooks"])
async def list_webhook_deliveries(
    webhook_id: str,
    request: _Request,
    limit: int = 50,
    offset: int = 0,
    event: _Opt[str] = None,
    status: _Opt[str] = None,
):
    """List delivery history for a specific webhook.

    Filter by ``event`` or ``status``.  Results are ordered newest-first.
    Only the authenticated account's webhooks are accessible.
    """
    account_id = _request_account_id(request)
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import Webhook as _WebhookModel, WebhookDelivery
        from sqlalchemy import select, func
        async with AsyncSessionLocal() as session:
            # Ownership check
            wh = (await session.execute(
                select(_WebhookModel).where(_WebhookModel.id == webhook_id)
            )).scalar_one_or_none()
            if wh is None:
                raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")
            if account_id and account_id != SUPERADMIN_ACCOUNT_ID and wh.account_id != account_id:
                raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")

            base = select(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook_id)
            count_q = select(func.count(WebhookDelivery.id)).where(WebhookDelivery.webhook_id == webhook_id)
            if event:
                base = base.where(WebhookDelivery.event == event)
                count_q = count_q.where(WebhookDelivery.event == event)
            if status:
                base = base.where(WebhookDelivery.status == status)
                count_q = count_q.where(WebhookDelivery.status == status)
            total = (await session.execute(count_q)).scalar() or 0
            result = await session.execute(
                base.order_by(WebhookDelivery.created_at.desc()).limit(limit).offset(offset)
            )
            rows = result.scalars().all()
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to list deliveries for webhook %s", webhook_id)
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

    model_config = {"json_schema_extra": {"examples": [
        {"events": ["bot.done", "bot.error"]},
        {"is_active": True},
        {"url": "https://api.acme.com/justheretolisten/v2"},
    ]}}


@router.patch("/{webhook_id}", response_model=WebhookResponse)
@_limiter.limit("10/minute")
async def update_webhook(webhook_id: str, payload: WebhookUpdate, request: _Request):
    """Update a registered webhook (URL, events, or active status).

    Use this to re-enable a webhook that was auto-disabled after consecutive failures.
    When re-enabling (`is_active: true`), `consecutive_failures` is reset to 0.
    """
    account_id = _request_account_id(request)
    wh = await _get_webhook_or_404(webhook_id, account_id)

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

    _spawn_audit(_audit_log(
        account_id=account_id,
        action="webhook.updated",
        resource_type="webhook",
        resource_id=webhook_id,
        ip_address=request.client.host if request.client else None,
        details=updates,
    ))

    return _to_response(wh)


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(webhook_id: str, request: _Request):
    """Return a single webhook by id.

    Returns 404 if the webhook does not exist or belongs to another account.
    Use this to inspect delivery counters and the current `is_active` flag.
    """
    wh = await _get_webhook_or_404(webhook_id, _request_account_id(request))
    return _to_response(wh)


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: str, request: _Request):
    """Delete a registered webhook permanently.

    Returns 204 on success. The webhook is removed from the in-memory store
    and the database, and all of its delivery records (pending, retrying, and
    historical) are deleted.
    """
    account_id = _request_account_id(request)
    # Ownership check before destructive op (raises 404 on tenant mismatch).
    await _get_webhook_or_404(webhook_id, account_id)
    if not await store.delete_webhook(webhook_id):
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")

    # Cascade-delete ALL delivery records for this webhook. There is no DB-level
    # foreign key from webhook_deliveries.webhook_id → webhooks.id, so nothing is
    # removed automatically: pending/retrying rows would keep the retry loop
    # firing ghost deliveries, and success/failed rows would be orphaned history
    # (unreachable now that the parent webhook 404s). Removing everything keeps
    # the table consistent with the in-memory store.
    try:
        from app.db import AsyncSessionLocal
        from app.models.account import WebhookDelivery
        from sqlalchemy import delete as _sa_delete
        async with AsyncSessionLocal() as session:
            await session.execute(
                _sa_delete(WebhookDelivery).where(
                    WebhookDelivery.webhook_id == webhook_id,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to clean up delivery records for deleted webhook %s", webhook_id)

    _spawn_audit(_audit_log(
        account_id=account_id,
        action="webhook.deleted",
        resource_type="webhook",
        resource_id=webhook_id,
    ))


@router.post(
    "/{webhook_id}/test",
    responses={200: {"content": {"application/json": {"example": {
        "status_code": 200,
        "url": "https://api.acme.com/justheretolisten/webhook",
    }}}}},
)
@_limiter.limit("5/minute")
async def test_webhook(webhook_id: str, request: _Request):
    """Send a test event to this webhook endpoint."""
    wh = await _get_webhook_or_404(webhook_id, _request_account_id(request))
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


@router.get(
    "/{webhook_id}/deliveries",
    responses={200: {"content": {"application/json": {"example": {
        "results": [{
            "id": "wd_3fbc81a0",
            "webhook_id": "wh_5fa921b7",
            "bot_id": "bot_8a72c5e1",
            "event": "bot.done",
            "status": "delivered",
            "attempt_number": 1,
            "response_status_code": 200,
            "delivered_at": "2026-05-04T15:34:18Z",
            "created_at": "2026-05-04T15:34:18Z",
        }],
        "total": 1,
        "limit": 50,
        "offset": 0,
        "has_more": False,
    }}}}},
)
async def list_deliveries(
    webhook_id: str,
    request: _Request,
    limit: int = 50,
    offset: int = 0,
):
    """List delivery log entries for a registered webhook.

    Returns a paginated envelope. Entries are sorted newest-first. Each entry
    includes the attempt status, HTTP response code, error message, and next retry time.
    """
    wh = await _get_webhook_or_404(webhook_id, _request_account_id(request))

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
