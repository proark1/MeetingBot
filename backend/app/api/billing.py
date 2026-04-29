"""Billing API — Stripe top-up, USDC deposits, and balance queries."""

import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_account_id, SUPERADMIN_ACCOUNT_ID
from app.models.account import Account, CreditTransaction

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["Billing"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    amount_usd: int = Field(
        description=(
            "Top-up amount in whole USD. Must be one of the values configured in "
            "`STRIPE_TOP_UP_AMOUNTS` (default: 10, 25, 50, 100)."
        ),
        examples=[25],
    )
    success_url: Optional[str] = Field(
        default=None,
        description=(
            "URL to redirect to after a successful payment. "
            "Defaults to `{base_url}/dashboard?payment=success`."
        ),
    )
    cancel_url: Optional[str] = Field(
        default=None,
        description=(
            "URL to redirect to if the user cancels the payment. "
            "Defaults to `{base_url}/topup?payment=cancelled`."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "amount_usd": 25,
                "success_url": "https://your-app.com/billing/success",
                "cancel_url": "https://your-app.com/billing/cancel",
            }
        }
    }


class CheckoutResponse(BaseModel):
    session_id: str = Field(description="Stripe Checkout session ID (`cs_...`).")
    checkout_url: str = Field(description="Redirect the user to this URL to complete payment.")
    amount_usd: int = Field(description="Amount that will be credited after successful payment.")


class SubscribeRequest(BaseModel):
    plan: str = Field(description="Target plan: 'starter', 'pro', or 'business'.")
    success_url: Optional[str] = Field(None, description="Redirect URL after successful subscription.")
    cancel_url: Optional[str] = Field(None, description="Redirect URL if user cancels.")


class SubscribeResponse(BaseModel):
    session_id: str = Field(description="Stripe Checkout session ID.")
    checkout_url: str = Field(description="Redirect the user to this URL.")
    plan: str = Field(description="Plan being subscribed to.")


class UsdcAddressResponse(BaseModel):
    deposit_address: str = Field(
        description="The Ethereum address to send USDC to (platform collection wallet or per-user HD address)."
    )
    your_wallet: Optional[str] = Field(
        default=None,
        description=(
            "Your registered sending wallet address. "
            "If using the platform wallet, you MUST send from this address so the system can identify you."
        ),
    )
    contract: str = Field(description="USDC ERC-20 token contract address on Ethereum mainnet.")
    network: str = Field(description="Blockchain network (always `Ethereum Mainnet (ERC-20)`).")
    note: str = Field(description="Additional instructions.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "deposit_address": "0xPlatformWallet...",
                "your_wallet": "0xYourRegisteredWallet...",
                "contract": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "network": "Ethereum Mainnet (ERC-20)",
                "note": "Send USDC from your registered wallet only. Credits are added automatically within ~1 minute.",
            }
        }
    }


class TransactionItem(BaseModel):
    id: str = Field(description="Unique transaction UUID.")
    amount_usd: float = Field(
        description=(
            "Amount in USD. Positive = credits added (top-up). "
            "Negative = credits deducted (bot usage)."
        )
    )
    type: str = Field(
        description=(
            "Transaction type. One of:\n"
            "- `stripe_topup` — credits added via Stripe card payment\n"
            "- `usdc_topup` — credits added via USDC deposit\n"
            "- `bot_usage` — credits deducted on bot completion (raw AI cost × `CREDIT_MARKUP`)"
        )
    )
    description: str = Field(description="Human-readable description of the transaction.")
    reference_id: Optional[str] = Field(
        default=None,
        description=(
            "External reference. For `stripe_topup`: Stripe session ID. "
            "For `usdc_topup`: Ethereum transaction hash. "
            "For `bot_usage`: bot UUID."
        ),
    )
    created_at: str = Field(description="ISO-8601 UTC timestamp when the transaction was recorded.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "amount_usd": -0.063,
                "type": "bot_usage",
                "description": "Bot usage: 45-min meeting (claude-sonnet-4-6)",
                "reference_id": "bot-uuid-here",
                "created_at": "2026-03-15T11:00:00Z",
            }
        }
    }


class BalanceResponse(BaseModel):
    credits_usd: float = Field(description="Current prepaid credit balance in USD.")
    transactions: list[TransactionItem] = Field(
        description="Last 50 transactions ordered by most recent first."
    )


# ── Helper ────────────────────────────────────────────────────────────────────

def _require_account(account_id: Optional[str]) -> str:
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(
            status_code=403,
            detail="Billing requires a per-user account. Register at POST /api/v1/auth/register",
        )
    return account_id


def _get_valid_amounts() -> list[int]:
    try:
        return [int(x.strip()) for x in settings.STRIPE_TOP_UP_AMOUNTS.split(",") if x.strip()]
    except ValueError:
        return [10, 25, 50, 100]


# ── Stripe ────────────────────────────────────────────────────────────────────

@router.post("/stripe/checkout", response_model=CheckoutResponse)
async def create_stripe_checkout(
    payload: CheckoutRequest,
    request: Request,
    account_id: Optional[str] = Depends(get_current_account_id),
):
    """
    Create a Stripe Checkout session to top up your credit balance via card payment.

    `amount_usd` must be one of the values in `STRIPE_TOP_UP_AMOUNTS` (default: 10, 25, 50, 100).

    Returns a `checkout_url` — redirect your user to that URL to complete payment.
    After a successful payment, credits are added to your balance automatically via
    the Stripe webhook (`POST /api/v1/billing/stripe/webhook`).

    Optional `success_url` and `cancel_url` override the default redirect destinations.
    """
    _require_account(account_id)

    valid_amounts = _get_valid_amounts()
    if payload.amount_usd not in valid_amounts:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid amount. Choose from: {valid_amounts}",
        )

    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=503,
            detail="Stripe payments are not configured on this server",
        )

    base_url = str(request.base_url).rstrip("/")
    success_url = payload.success_url or f"{base_url}/dashboard?payment=success"
    cancel_url = payload.cancel_url or f"{base_url}/topup?payment=cancelled"

    from app.services.stripe_service import create_checkout_session, record_stripe_session
    session_id, checkout_url = create_checkout_session(
        account_id=account_id,
        amount_usd=payload.amount_usd,
        success_url=success_url,
        cancel_url=cancel_url,
    )

    await record_stripe_session(session_id, account_id, payload.amount_usd)

    return CheckoutResponse(
        session_id=session_id,
        checkout_url=checkout_url,
        amount_usd=payload.amount_usd,
    )


@router.post("/subscribe", response_model=SubscribeResponse)
async def create_subscription(
    payload: SubscribeRequest,
    request: Request,
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """Create a Stripe subscription checkout for a plan upgrade."""
    if not account_id or account_id == SUPERADMIN_ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Requires per-user authentication")
    if payload.plan not in ("starter", "pro", "business"):
        raise HTTPException(status_code=422, detail="Plan must be starter, pro, or business")
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe payments not configured")

    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    price_map = {
        "starter": settings.STRIPE_STARTER_PRICE_ID,
        "pro": settings.STRIPE_PRO_PRICE_ID,
        "business": settings.STRIPE_BUSINESS_PRICE_ID,
    }
    price_id = price_map.get(payload.plan)
    if not price_id:
        raise HTTPException(status_code=503, detail=f"Stripe Price ID not configured for plan '{payload.plan}'")

    import stripe as _stripe
    _stripe.api_key = settings.STRIPE_SECRET_KEY

    base_url = str(request.base_url).rstrip("/")
    success_url = payload.success_url or f"{base_url}/dashboard?subscription=success"
    cancel_url = payload.cancel_url or f"{base_url}/dashboard?subscription=cancelled"

    params: dict = {
        "payment_method_types": ["card"],
        "line_items": [{"price": price_id, "quantity": 1}],
        "mode": "subscription",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"account_id": account_id, "plan": payload.plan},
    }
    if account.stripe_customer_id:
        params["customer"] = account.stripe_customer_id
    else:
        params["customer_email"] = account.email

    session = _stripe.checkout.Session.create(**params)

    # Persist Stripe customer ID if newly created
    if session.get("customer") and not account.stripe_customer_id:
        account.stripe_customer_id = session["customer"]
        await db.commit()

    return SubscribeResponse(
        session_id=session.id,
        checkout_url=session.url,
        plan=payload.plan,
    )


@router.post("/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request):
    """
    Stripe webhook receiver — verifies HMAC signature and credits accounts on payment.
    Register this URL in your Stripe dashboard as a webhook endpoint.
    Event type: checkout.session.completed
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Stripe webhook not configured")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        from app.services.stripe_service import verify_webhook, handle_checkout_completed
        event = verify_webhook(payload, sig_header)
    except Exception as exc:
        logger.warning("Stripe webhook signature verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if session.get("mode") == "subscription":
            # Subscription checkout — link Stripe customer and activate plan.
            acct_id = session.get("metadata", {}).get("account_id")
            plan = session.get("metadata", {}).get("plan", "free")
            customer_id = session.get("customer")
            subscription_id = session.get("subscription")  # persist for later cancel/renew lookups
            # Validate that the metadata.plan actually matches the price the customer paid for.
            # Defends against any future code path that creates a Session with mismatched metadata.
            expected_price = {
                "starter": settings.STRIPE_STARTER_PRICE_ID,
                "pro": settings.STRIPE_PRO_PRICE_ID,
                "business": settings.STRIPE_BUSINESS_PRICE_ID,
            }.get(plan)
            paid_price_id = None
            try:
                line_items = session.get("line_items") or {}
                items = line_items.get("data") if isinstance(line_items, dict) else None
                if items:
                    paid_price_id = (items[0].get("price") or {}).get("id")
            except Exception:
                paid_price_id = None
            if expected_price and paid_price_id and paid_price_id != expected_price:
                logger.error(
                    "Stripe webhook rejected: metadata.plan=%s implies price=%s but session paid price=%s",
                    plan, expected_price, paid_price_id,
                )
                return {"received": True, "ignored": "plan/price mismatch"}
            if acct_id and customer_id:
                from app.db import AsyncSessionLocal
                from datetime import datetime, timezone, timedelta
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(Account).where(Account.id == acct_id))
                    account = result.scalar_one_or_none()
                    if account:
                        account.stripe_customer_id = customer_id
                        if subscription_id:
                            account.stripe_subscription_id = subscription_id
                        account.plan = plan
                        account.monthly_bots_used = 0
                        account.monthly_reset_at = datetime.now(timezone.utc) + timedelta(days=30)
                        await db.commit()
                        logger.info(
                            "Subscription activated: account %s → plan %s (sub %s)",
                            acct_id, plan, subscription_id,
                        )
        else:
            await handle_checkout_completed(session)

    elif event["type"] == "invoice.paid":
        # Recurring subscription payment — keep plan active and reset the
        # monthly bot counter / reset date so paying customers don't hit
        # their plan ceiling on day 31.
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        if customer_id:
            from app.db import AsyncSessionLocal
            from datetime import datetime, timezone, timedelta
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Account).where(Account.stripe_customer_id == customer_id))
                account = result.scalar_one_or_none()
                if account:
                    period_end_unix = invoice.get("lines", {}).get("data", [{}])[0].get("period", {}).get("end")
                    if period_end_unix:
                        next_reset = datetime.fromtimestamp(period_end_unix, tz=timezone.utc)
                    else:
                        next_reset = datetime.now(timezone.utc) + timedelta(days=30)
                    account.monthly_bots_used = 0
                    account.monthly_reset_at = next_reset
                    await db.commit()
                    logger.info(
                        "Subscription renewed: account %s, plan %s, reset %s",
                        account.id, account.plan, next_reset.isoformat(),
                    )

    elif event["type"] == "customer.subscription.deleted":
        # Subscription cancelled — downgrade to free
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        if sub_id:
            from app.db import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Account).where(Account.stripe_subscription_id == sub_id))
                account = result.scalar_one_or_none()
                if account:
                    account.plan = "free"
                    account.stripe_subscription_id = None
                    await db.commit()
                    logger.info("Subscription cancelled: account %s downgraded to free", account.id)

    return {"received": True}


# ── USDC ──────────────────────────────────────────────────────────────────────

@router.get("/usdc/address", response_model=UsdcAddressResponse)
async def get_usdc_address(
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get the USDC deposit address and instructions.

    **Platform wallet mode** (preferred): The admin sets a single collection wallet.
    You must first register your own Ethereum wallet via `PUT /api/v1/auth/wallet`
    so the system can match incoming transfers to your account by the `from` address.

    **HD wallet mode** (legacy fallback): If no platform wallet is configured,
    each user gets a unique deposit address derived from an HD seed.

    1 USDC = $1.00 credit. Credits are added automatically within ~1 minute.
    """
    _require_account(account_id)

    # Load the user's account to check their registered wallet
    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # First try the admin-configured platform wallet
    from app.models.account import PlatformConfig
    from app.api.admin import WALLET_KEY
    wallet_result = await db.execute(
        select(PlatformConfig).where(PlatformConfig.key == WALLET_KEY)
    )
    wallet_config = wallet_result.scalar_one_or_none()

    if wallet_config and wallet_config.value:
        if not account.wallet_address:
            note = (
                "IMPORTANT: You must register your Ethereum wallet first via "
                "PUT /api/v1/auth/wallet (or the dashboard). Without a registered wallet, "
                "the system cannot attribute your USDC deposit to your account."
            )
        else:
            note = (
                f"Send USDC from your registered wallet ({account.wallet_address}) only. "
                "Other tokens will not be credited. "
                "Credits are added automatically within ~1 minute after confirmation."
            )

        return UsdcAddressResponse(
            deposit_address=wallet_config.value,
            your_wallet=account.wallet_address,
            contract=settings.USDC_CONTRACT,
            network="Ethereum Mainnet (ERC-20)",
            note=note,
        )

    # Fallback to HD-derived per-user address
    if not settings.CRYPTO_HD_SEED:
        raise HTTPException(
            status_code=503,
            detail="Crypto payments are not configured on this server",
        )

    from app.services.crypto_service import get_or_create_deposit_address
    address = await get_or_create_deposit_address(account_id, db)

    return UsdcAddressResponse(
        deposit_address=address,
        your_wallet=account.wallet_address,
        contract=settings.USDC_CONTRACT,
        network="Ethereum Mainnet (ERC-20)",
        note=(
            "Send USDC only. Other tokens will not be credited. "
            "Credits are added automatically within ~1 minute after confirmation."
        ),
    )


# ── Balance ───────────────────────────────────────────────────────────────────

@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    account_id: Optional[str] = Depends(get_current_account_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get current credit balance and transaction history.

    Returns `credits_usd` (current balance) and the last 50 transactions ordered
    most-recent-first. Transaction `type` values:
    - `stripe_topup` — credits added via Stripe card payment
    - `usdc_topup` — credits added via USDC on-chain deposit
    - `bot_usage` — credits deducted on bot completion (raw AI cost × `CREDIT_MARKUP`)
    """
    _require_account(account_id)

    result = await db.execute(select(Account).where(Account.id == account_id))
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    txns_result = await db.execute(
        select(CreditTransaction)
        .where(CreditTransaction.account_id == account_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(50)
    )
    txns = txns_result.scalars().all()

    return BalanceResponse(
        credits_usd=float(account.credits_usd or 0),
        transactions=[
            TransactionItem(
                id=t.id,
                amount_usd=float(t.amount_usd),
                type=t.type,
                description=t.description,
                reference_id=t.reference_id,
                created_at=t.created_at.isoformat(),
            )
            for t in txns
        ],
    )
