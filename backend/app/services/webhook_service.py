"""Webhook delivery service — fires HTTP POST to registered endpoints."""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.webhook import Webhook

logger = logging.getLogger(__name__)


async def dispatch_event(
    db: AsyncSession,
    event: str,
    payload: dict,
) -> None:
    """Send an event to all active webhooks that subscribe to it."""
    result = await db.execute(
        select(Webhook).where(Webhook.is_active == True)  # noqa: E712
    )
    webhooks = result.scalars().all()

    body = json.dumps({"event": event, "data": payload, "ts": datetime.now(timezone.utc).isoformat()})

    async with httpx.AsyncClient(timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as client:
        for wh in webhooks:
            subscribed = wh.events == "*" or event in wh.events.split(",")
            if not subscribed:
                continue

            headers = {"Content-Type": "application/json"}
            if wh.secret:
                sig = hmac.new(
                    wh.secret.encode(), body.encode(), hashlib.sha256
                ).hexdigest()
                headers["X-MeetingBot-Signature"] = f"sha256={sig}"

            try:
                resp = await client.post(wh.url, content=body, headers=headers)
                wh.last_delivery_status = resp.status_code
                logger.info("Webhook %s → %s  %s", event, wh.url, resp.status_code)
            except Exception as exc:
                wh.last_delivery_status = None
                logger.warning("Webhook delivery failed for %s: %s", wh.url, exc)

            wh.delivery_attempts += 1
            wh.last_delivery_at = datetime.utcnow()

    await db.commit()
