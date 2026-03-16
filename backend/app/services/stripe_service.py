"""Stripe payment integration for credit top-ups."""

import logging
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


def _get_stripe():
    """Import and configure stripe lazily to avoid import errors when not installed."""
    try:
        import stripe
    except ImportError:
        raise RuntimeError("stripe package not installed. Run: pip install stripe")
    from app.config import settings
    if not settings.STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def create_checkout_session(
    account_id: str,
    amount_usd: int,
    success_url: str,
    cancel_url: str,
) -> tuple[str, str]:
    """
    Create a Stripe Checkout session.
    Returns (session_id, checkout_url).
    """
    stripe = _get_stripe()
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": amount_usd * 100,  # Stripe uses cents
                    "product_data": {
                        "name": f"MeetingBot Credits — ${amount_usd}",
                        "description": f"Add ${amount_usd} to your MeetingBot credit balance",
                    },
                },
                "quantity": 1,
            }
        ],
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"account_id": account_id, "amount_usd": str(amount_usd)},
    )
    return session.id, session.url


def verify_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify Stripe webhook signature and return the parsed event."""
    stripe = _get_stripe()
    from app.config import settings
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not configured")
    event = stripe.Webhook.construct_event(
        payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
    )
    return event


def record_stripe_session(session_id: str, account_id: str, amount_usd: int) -> None:
    """Store a pending Stripe top-up record in the database."""
    import asyncio
    from decimal import Decimal
    from app.db import AsyncSessionLocal
    from app.models.account import StripeTopUp
    import uuid

    async def _save():
        async with AsyncSessionLocal() as db:
            topup = StripeTopUp(
                id=str(uuid.uuid4()),
                account_id=account_id,
                stripe_session_id=session_id,
                amount_usd=Decimal(str(amount_usd)),
                status="pending",
            )
            db.add(topup)
            await db.commit()

    asyncio.create_task(_save())


async def handle_checkout_completed(session: dict) -> Optional[Decimal]:
    """
    Handle a completed Stripe Checkout session:
    - Marks the StripeTopUp as completed
    - Adds credits to the account
    Returns the amount credited, or None if already processed.
    """
    from decimal import Decimal
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.models.account import StripeTopUp
    from app.services.credit_service import add_credits

    stripe_session_id = session.get("id", "")
    metadata = session.get("metadata", {})
    account_id = metadata.get("account_id", "")
    amount_usd_str = metadata.get("amount_usd", "0")

    if not account_id or not stripe_session_id:
        logger.warning("Stripe webhook: missing account_id or session_id in metadata")
        return None

    amount_usd = Decimal(amount_usd_str)

    async with AsyncSessionLocal() as db:
        # Check for duplicate processing
        result = await db.execute(
            select(StripeTopUp).where(StripeTopUp.stripe_session_id == stripe_session_id)
        )
        topup = result.scalar_one_or_none()

        if topup and topup.status == "completed":
            logger.info("Stripe session %s already processed, skipping", stripe_session_id)
            return None

        if topup is None:
            import uuid
            topup = StripeTopUp(
                id=str(uuid.uuid4()),
                account_id=account_id,
                stripe_session_id=stripe_session_id,
                amount_usd=amount_usd,
                status="pending",
            )
            db.add(topup)

        topup.status = "completed"
        await db.commit()

        await add_credits(
            account_id=account_id,
            amount_usd=amount_usd,
            type="stripe_topup",
            description=f"Stripe payment: ${amount_usd:.2f}",
            reference_id=stripe_session_id,
            db=db,
        )

    logger.info("Stripe top-up completed: +$%.2f to account %s", amount_usd, account_id)
    return amount_usd
