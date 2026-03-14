"""Billing service — Stripe integration for API and platform billing.

Supports two billing models:
  1. **API billing** — metered usage charged via Stripe usage-based subscriptions
  2. **Platform billing** — per-meeting or per-token charges for self-serve users

Environment variables (see config.py):
  STRIPE_SECRET_KEY        — Stripe API key
  STRIPE_WEBHOOK_SECRET    — Stripe webhook signature secret
  STRIPE_PRICE_PER_MEETING — flat fee per meeting in cents (0 = disabled)
  STRIPE_PRICE_PER_1K_TOKENS — per-1K-token fee in cents (0 = disabled)
  BILLING_COST_MARKUP      — multiplier on raw AI cost (e.g. 2.0 = 2×)
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _get_stripe():
    """Lazy-import stripe and configure it."""
    from app.config import settings
    if not settings.STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    try:
        import stripe
    except ImportError:
        raise RuntimeError("stripe is not installed — run: pip install stripe")
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


# ── Customer management ──────────────────────────────────────────────────────

async def get_or_create_customer(email: str, name: str | None = None, metadata: dict | None = None) -> dict[str, Any]:
    """Find an existing Stripe customer by email, or create one."""
    stripe = _get_stripe()
    customers = stripe.Customer.list(email=email, limit=1)
    if customers.data:
        return customers.data[0]
    return stripe.Customer.create(
        email=email,
        name=name or email,
        metadata=metadata or {},
    )


# ── Checkout / payment ───────────────────────────────────────────────────────

async def create_checkout_session(
    customer_email: str,
    bot_id: str,
    success_url: str,
    cancel_url: str,
    amount_cents: int | None = None,
    description: str = "MeetingBot session",
) -> dict[str, Any]:
    """Create a Stripe Checkout Session for a one-time meeting charge.

    If amount_cents is None, it is computed from the bot's AI usage.
    Returns the Checkout Session object (contains .url for redirect).
    """
    stripe = _get_stripe()

    customer = await get_or_create_customer(customer_email)

    session = stripe.checkout.Session.create(
        customer=customer["id"],
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": amount_cents or 0,
                "product_data": {
                    "name": description,
                    "metadata": {"bot_id": bot_id},
                },
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"bot_id": bot_id},
    )
    return session


async def create_usage_subscription(
    customer_email: str,
    success_url: str,
    cancel_url: str,
) -> dict[str, Any]:
    """Create a Stripe Checkout Session for a metered usage subscription.

    This sets up a recurring subscription where usage (meetings/tokens)
    is reported via Stripe Usage Records.
    """
    stripe = _get_stripe()
    from app.config import settings

    customer = await get_or_create_customer(customer_email)

    # Create or retrieve the metered price
    prices = stripe.Price.list(
        lookup_keys=["meetingbot_usage"],
        limit=1,
    )
    if prices.data:
        price_id = prices.data[0]["id"]
    else:
        product = stripe.Product.create(
            name="MeetingBot API Usage",
            metadata={"service": "meetingbot"},
        )
        price = stripe.Price.create(
            product=product["id"],
            currency="usd",
            recurring={"interval": "month", "usage_type": "metered"},
            unit_amount=settings.STRIPE_PRICE_PER_1K_TOKENS or 1,
            lookup_key="meetingbot_usage",
        )
        price_id = price["id"]

    session = stripe.checkout.Session.create(
        customer=customer["id"],
        payment_method_types=["card"],
        line_items=[{"price": price_id}],
        mode="subscription",
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return session


async def report_usage(subscription_item_id: str, quantity: int, bot_id: str = "") -> dict[str, Any]:
    """Report metered usage to Stripe for a subscription item."""
    import time
    stripe = _get_stripe()
    return stripe.SubscriptionItem.create_usage_record(
        subscription_item_id,
        quantity=quantity,
        timestamp=int(time.time()),
        action="increment",
        idempotency_key=f"bot_{bot_id}" if bot_id else None,
    )


# ── Cost calculation ─────────────────────────────────────────────────────────

def calculate_meeting_charge(
    ai_total_cost_usd: float,
    ai_total_tokens: int,
) -> dict[str, Any]:
    """Calculate the charge for a meeting based on AI usage.

    Returns a breakdown with raw cost, markup, and final charge.
    """
    from app.config import settings

    raw_cost = ai_total_cost_usd
    markup = settings.BILLING_COST_MARKUP
    marked_up_cost = round(raw_cost * markup, 6)

    # Per-meeting flat fee (converted from cents to USD)
    flat_fee = settings.STRIPE_PRICE_PER_MEETING / 100.0

    # Per-token fee
    token_fee_rate = settings.STRIPE_PRICE_PER_1K_TOKENS / 100.0  # cents → USD per 1K tokens
    token_fee = round((ai_total_tokens / 1000.0) * token_fee_rate, 6)

    total_charge = round(flat_fee + max(marked_up_cost, token_fee), 4)

    return {
        "raw_ai_cost_usd": round(raw_cost, 6),
        "markup_multiplier": markup,
        "marked_up_cost_usd": marked_up_cost,
        "flat_fee_usd": flat_fee,
        "token_fee_usd": token_fee,
        "total_charge_usd": total_charge,
        "total_charge_cents": int(total_charge * 100),
        "total_tokens": ai_total_tokens,
    }


# ── Webhook handling ─────────────────────────────────────────────────────────

async def handle_stripe_webhook(payload: bytes, sig_header: str) -> dict[str, Any]:
    """Verify and process a Stripe webhook event.

    Returns the parsed event for the caller to act on.
    """
    stripe = _get_stripe()
    from app.config import settings

    if not settings.STRIPE_WEBHOOK_SECRET:
        raise ValueError("STRIPE_WEBHOOK_SECRET is not configured")

    event = stripe.Webhook.construct_event(
        payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
    )

    event_type = event["type"]
    logger.info("Stripe webhook: %s", event_type)

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        bot_id = session.get("metadata", {}).get("bot_id")
        logger.info("Payment completed for bot %s (session %s)", bot_id, session["id"])

    elif event_type == "invoice.paid":
        invoice = event["data"]["object"]
        logger.info("Invoice paid: %s (customer %s)", invoice["id"], invoice["customer"])

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        logger.warning("Payment failed: %s (customer %s)", invoice["id"], invoice["customer"])

    return {"event_type": event_type, "event_id": event["id"]}
