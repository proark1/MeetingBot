"""Webhook registration API."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.webhook import Webhook
from app.schemas.webhook import WebhookCreate, WebhookResponse

router = APIRouter(prefix="/webhook", tags=["Webhooks"])


def _to_response(wh: Webhook) -> WebhookResponse:
    return WebhookResponse(
        id=wh.id,
        url=wh.url,
        events=wh.events.split(",") if wh.events else ["*"],
        is_active=wh.is_active,
        created_at=wh.created_at,
        delivery_attempts=wh.delivery_attempts,
        last_delivery_at=wh.last_delivery_at,
        last_delivery_status=wh.last_delivery_status,
    )


@router.post("", response_model=WebhookResponse, status_code=201)
async def create_webhook(
    payload: WebhookCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    wh = Webhook(
        url=payload.url,
        events=",".join(payload.events),
        secret=payload.secret,
    )
    db.add(wh)
    await db.commit()
    await db.refresh(wh)
    return _to_response(wh)


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(select(Webhook).order_by(Webhook.created_at.desc()))
    return [_to_response(wh) for wh in result.scalars().all()]


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    webhook_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    wh = await _get_or_404(db, webhook_id)
    return _to_response(wh)


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    wh = await _get_or_404(db, webhook_id)
    await db.delete(wh)
    await db.commit()


@router.post("/{webhook_id}/test")
async def test_webhook(
    webhook_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Send a test delivery to this webhook endpoint and return the HTTP status code."""
    wh = await _get_or_404(db, webhook_id)
    from app.services.webhook_service import _get_client, _deliver_with_retry
    import hashlib, hmac, json
    body = json.dumps({
        "event": "bot.test",
        "data": {"message": "Test delivery from MeetingBot"},
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    headers = {"Content-Type": "application/json", "User-Agent": "MeetingBot/1.0"}
    if wh.secret:
        sig = hmac.new(wh.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        headers["X-MeetingBot-Signature"] = f"sha256={sig}"
    client = _get_client()
    status_code = await _deliver_with_retry(client, wh.url, body, headers)
    if status_code is None:
        raise HTTPException(status_code=502, detail="Test delivery failed — endpoint unreachable or returned 5xx")
    return {"status_code": status_code, "url": wh.url}


async def _get_or_404(db: AsyncSession, webhook_id: str) -> Webhook:
    result = await db.execute(select(Webhook).where(Webhook.id == webhook_id))
    wh = result.scalar_one_or_none()
    if wh is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id!r} not found")
    return wh
