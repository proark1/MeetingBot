"""Billing API — usage tracking and Stripe integration."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.bot import Bot
from app.services import billing_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["Billing"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class UsageSummary(BaseModel):
    total_meetings: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_charge_usd: float = 0.0
    avg_tokens_per_meeting: float = 0.0
    avg_cost_per_meeting: float = 0.0
    primary_model: str | None = None
    by_model: dict[str, Any] = {}


class MeetingCharge(BaseModel):
    bot_id: str
    raw_ai_cost_usd: float = 0.0
    markup_multiplier: float = 1.0
    marked_up_cost_usd: float = 0.0
    flat_fee_usd: float = 0.0
    token_fee_usd: float = 0.0
    total_charge_usd: float = 0.0
    total_charge_cents: int = 0
    total_tokens: int = 0


class CheckoutRequest(BaseModel):
    customer_email: str
    bot_id: str | None = None
    success_url: str = Field(description="URL to redirect after successful payment.")
    cancel_url: str = Field(description="URL to redirect if payment is cancelled.")


class SubscriptionRequest(BaseModel):
    customer_email: str
    success_url: str
    cancel_url: str


# ── GET /api/v1/billing/usage ────────────────────────────────────────────────

@router.get("/usage", response_model=UsageSummary)
async def get_usage_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(default=None, description="Filter by bot status (e.g. 'done')"),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Get aggregated AI usage across all meetings.

    Returns total tokens, cost, and per-model breakdown.
    """
    query = select(Bot).order_by(Bot.created_at.desc()).limit(limit)
    if status:
        query = query.where(Bot.status == status)

    bots = (await db.execute(query)).scalars().all()

    total_tokens = 0
    total_cost = 0.0
    model_tokens: dict[str, int] = {}
    model_cost: dict[str, float] = {}
    meetings_with_usage = 0

    for bot in bots:
        if bot.ai_total_tokens:
            meetings_with_usage += 1
            total_tokens += bot.ai_total_tokens or 0
            total_cost += bot.ai_total_cost_usd or 0.0

            for record in (bot.ai_usage or []):
                model = record.get("model", "unknown")
                model_tokens[model] = model_tokens.get(model, 0) + record.get("total_tokens", 0)
                model_cost[model] = model_cost.get(model, 0.0) + record.get("cost_usd", 0.0)

    # Calculate charges
    charge = billing_service.calculate_meeting_charge(total_cost, total_tokens)

    primary_model = max(model_tokens, key=model_tokens.get) if model_tokens else None

    return UsageSummary(
        total_meetings=meetings_with_usage,
        total_tokens=total_tokens,
        total_cost_usd=round(total_cost, 4),
        total_charge_usd=charge["total_charge_usd"],
        avg_tokens_per_meeting=round(total_tokens / max(meetings_with_usage, 1), 0),
        avg_cost_per_meeting=round(total_cost / max(meetings_with_usage, 1), 4),
        primary_model=primary_model,
        by_model={
            m: {"tokens": model_tokens.get(m, 0), "cost_usd": round(model_cost.get(m, 0.0), 6)}
            for m in model_tokens
        },
    )


# ── GET /api/v1/billing/meeting/{bot_id} ─────────────────────────────────────

@router.get("/meeting/{bot_id}", response_model=MeetingCharge)
async def get_meeting_charge(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get the billing breakdown for a specific meeting."""
    result = await db.execute(select(Bot).where(Bot.id == bot_id))
    bot = result.scalar_one_or_none()
    if bot is None:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")

    charge = billing_service.calculate_meeting_charge(
        bot.ai_total_cost_usd or 0.0,
        bot.ai_total_tokens or 0,
    )

    return MeetingCharge(bot_id=bot_id, **charge)


# ── POST /api/v1/billing/checkout ────────────────────────────────────────────

@router.post("/checkout")
async def create_checkout(
    payload: CheckoutRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a Stripe Checkout Session for a meeting charge.

    If bot_id is provided, the charge is computed from the bot's AI usage.
    Redirect the user to the returned `checkout_url`.
    """
    from app.config import settings
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    amount_cents = 0
    description = "MeetingBot session"

    if payload.bot_id:
        result = await db.execute(select(Bot).where(Bot.id == payload.bot_id))
        bot = result.scalar_one_or_none()
        if bot is None:
            raise HTTPException(status_code=404, detail=f"Bot {payload.bot_id!r} not found")

        charge = billing_service.calculate_meeting_charge(
            bot.ai_total_cost_usd or 0.0,
            bot.ai_total_tokens or 0,
        )
        amount_cents = charge["total_charge_cents"]
        description = f"MeetingBot — {bot.meeting_platform} ({bot.ai_total_tokens or 0} tokens)"

    try:
        session = await billing_service.create_checkout_session(
            customer_email=payload.customer_email,
            bot_id=payload.bot_id or "",
            success_url=payload.success_url,
            cancel_url=payload.cancel_url,
            amount_cents=amount_cents,
            description=description,
        )
        return {"checkout_url": session["url"], "session_id": session["id"]}
    except Exception as exc:
        logger.error("Stripe checkout error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /api/v1/billing/subscribe ───────────────────────────────────────────

@router.post("/subscribe")
async def create_subscription(payload: SubscriptionRequest):
    """Create a Stripe Checkout Session for a metered usage subscription.

    The subscription uses Stripe's usage-based billing — usage is reported
    automatically after each meeting completes.
    """
    from app.config import settings
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    try:
        session = await billing_service.create_usage_subscription(
            customer_email=payload.customer_email,
            success_url=payload.success_url,
            cancel_url=payload.cancel_url,
        )
        return {"checkout_url": session["url"], "session_id": session["id"]}
    except Exception as exc:
        logger.error("Stripe subscription error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /api/v1/billing/webhook ─────────────────────────────────────────────

@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events (payment confirmations, failures, etc.).

    This endpoint should be configured in the Stripe dashboard as the webhook URL.
    """
    from app.config import settings
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Stripe webhooks not configured")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        result = await billing_service.handle_stripe_webhook(payload, sig)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Stripe webhook error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
