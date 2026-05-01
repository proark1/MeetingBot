"""Stripe payment integration for credit top-ups."""

import asyncio
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


async def create_checkout_session(
    account_id: str,
    amount_usd: int,
    success_url: str,
    cancel_url: str,
) -> tuple[str, str]:
    """Create a Stripe Checkout session — runs the blocking SDK call in a thread.

    Returns (session_id, checkout_url).
    """
    stripe = _get_stripe()

    def _create():
        return stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": amount_usd * 100,  # Stripe uses cents
                        "product_data": {
                            "name": f"JustHereToListen.io Credits — ${amount_usd}",
                            "description": f"Add ${amount_usd} to your JustHereToListen.io credit balance",
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

    session = await asyncio.to_thread(_create)
    return session.id, session.url


async def verify_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify Stripe webhook signature and return the parsed event.

    Runs the (CPU-light but blocking) signature verification in a worker
    thread so the event loop isn't held during HMAC compare and JSON parse.
    """
    stripe = _get_stripe()
    from app.config import settings
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not configured")
    event = await asyncio.to_thread(
        stripe.Webhook.construct_event,
        payload, sig_header, settings.STRIPE_WEBHOOK_SECRET,
    )
    return event


async def record_stripe_session(session_id: str, account_id: str, amount_usd: int) -> None:
    """Store a pending Stripe top-up record in the database."""
    from decimal import Decimal
    from app.db import AsyncSessionLocal
    from app.models.account import StripeTopUp
    import uuid

    async with AsyncSessionLocal() as db:
        topup = StripeTopUp(
            id=str(uuid.uuid4()),
            account_id=account_id,
            stripe_session_id=session_id,
            amount_usd=Decimal(str(amount_usd)),
            status="pending",
        )
        db.add(topup)
        try:
            await db.commit()
        except Exception:
            logger.exception(
                "Failed to record Stripe session %s for account %s — credits will still "
                "be applied when the webhook arrives",
                session_id, account_id,
            )


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

    if not account_id or not stripe_session_id:
        logger.warning("Stripe webhook: missing account_id or session_id in metadata")
        return None

    # Use the amount Stripe actually charged (amount_total is in cents) as the
    # authoritative value — fall back to metadata only if the field is absent.
    amount_total_cents = session.get("amount_total")
    if amount_total_cents is not None:
        amount_usd = Decimal(str(amount_total_cents)) / 100
    else:
        amount_usd = Decimal(metadata.get("amount_usd", "0"))

    async with AsyncSessionLocal() as db:
        # Lock the row for the entire credit transaction so concurrent webhook
        # retries serialise here. Without ``with_for_update()``, two deliveries
        # of the same ``checkout.session.completed`` could both observe
        # ``status == "pending"``, both flip it to ``completed``, and both call
        # ``add_credits`` — double-crediting the account.
        result = await db.execute(
            select(StripeTopUp)
            .where(StripeTopUp.stripe_session_id == stripe_session_id)
            .with_for_update()
        )
        topup = result.scalar_one_or_none()

        if topup and topup.status == "completed":
            logger.info("Stripe session %s already processed, skipping", stripe_session_id)
            return None

        if topup is None:
            # First time we've seen this session — insert a row to claim it.
            # ``stripe_session_id`` carries a unique constraint, so a concurrent
            # delivery racing past the SELECT will get IntegrityError on commit
            # and back off without crediting.
            import uuid
            topup = StripeTopUp(
                id=str(uuid.uuid4()),
                account_id=account_id,
                stripe_session_id=stripe_session_id,
                amount_usd=amount_usd,
                status="completed",
            )
            db.add(topup)
            try:
                await db.flush()
            except Exception as exc:
                # Concurrent webhook delivery beat us to the insert.
                await db.rollback()
                logger.info(
                    "Stripe session %s claimed by concurrent delivery (%s) — skipping",
                    stripe_session_id, exc.__class__.__name__,
                )
                return None
        else:
            topup.status = "completed"

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
